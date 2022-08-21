from datetime import datetime, timedelta
import pytz
import requests
from redbot.core import commands
from redbot.core.utils.chat_formatting import text_to_file
import discord

SCHEDULE_ENDPOINT = "https://esports-api.lolesports.com/persisted/gw/getSchedule?hl=en-US&leagueId={}"
NEXT_PAGE = "&pageToken={}"
LEAGUES_ENDPOINT = "https://esports-api.lolesports.com/persisted/gw/getLeagues?hl=en-US"

START = """== {0} ==
{{{{SetPatch|patch= |disabled= |hotfix= |footnote=}}}}
{{{{MatchSchedule/Start|tab={0} |bestof={1} |shownname={2} }}}}\n"""
MATCH = """{{{{MatchSchedule|bestof={best_of} |team1={t1} |team2={t2} |team1score= |team2score= |winner=
|date={date} |time={time} |timezone={timezone} |dst={dst} |pbp= |color= |vodinterview= |with= |stream={stream}
{games}}}}}\n"""
GAME = """|game{}={{{{MatchSchedule/Game\n|blue= |red= |winner= |ssel= |ff=\n|riot_platform_game_id=\n|recap=\n|vodpb=
|vodstart=\n|vodpost=\n|vodhl=\n|vodinterview=\n|with=\n|mvp=\n}}}}\n"""
END = "{{MatchSchedule/End}}\n\n"

ERROR_MESSAGE = "An error has occured. {} might not exist. If your input contains spaces, try again using quotes!"


class MatchScheduleParser(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.group()
    async def lolesportsparser(self, ctx):
        """Commands to parse lolesports match schedules"""

    @lolesportsparser.command()
    async def parse(self, ctx, tournament, shownname="", stream=""):
        try:
            schedule = run(tournament, shownname, stream)
        except TypeError:
            try:
                schedule = get_schedule(tournament.upper(), shownname, stream)
            except TypeError:
                await ctx.send(ERROR_MESSAGE.format(tournament))
                return
        await ctx.author.send(file=text_to_file(schedule, filename="matchschedule.txt"))
        if not isinstance(ctx.channel, discord.channel.DMChannel):
            await ctx.send("Check your DMs!")

    @lolesportsparser.command()
    async def list(self, ctx):
        leagues = get_leagues()
        await ctx.send(leagues)


def get_headers():
    headers = {"x-api-key": "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"}
    return headers


def get_one_schedule(league_id, headers, newer):
    if newer:
        endpoint = SCHEDULE_ENDPOINT.format(league_id) + NEXT_PAGE.format(newer)
    else:
        endpoint = SCHEDULE_ENDPOINT.format(league_id)
    schedule = requests.get(endpoint, headers=headers).json()
    return schedule


def get_schedules(league_id, headers):
    schedule_full = []
    newer = None
    while True:
        schedule = get_one_schedule(league_id, headers, newer)
        schedule_full.append(schedule)
        newer = schedule["data"]["schedule"]["pages"]["newer"]
        if not newer:
            break
    return schedule_full


def get_leagues():
    headers = get_headers()
    leagues = "Leagues available on lolesports.com are:\n```"
    json_leagues = requests.get(LEAGUES_ENDPOINT, headers=headers).json()["data"]["leagues"]
    print(json_leagues)
    for league in json_leagues:
        leagues += league["name"] + "\n"
    leagues += "```"
    return leagues


def get_league(league_name, headers):
    leagues = requests.get(LEAGUES_ENDPOINT, headers=headers).json()["data"]["leagues"]
    league_id = None
    for league in leagues:
        if league["slug"] == league_name or league["name"] == league_name:
            league_id = league["id"]
    return league_id


def parse_schedule(schedule, shownname, stream):
    output, title = "", ""
    for page in schedule:
        page_schedule = page["data"]["schedule"]["events"]
        for match in page_schedule:
            if match["type"] != "match":
                continue
            start_time = match["startTime"]
            start_datetime = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S%z")
            pst_object = start_datetime.astimezone(pytz.timezone("PST8PDT"))
            if pst_object.dst():
                dst = "spring"
            else:
                dst = "no"
            start_date = pst_object.strftime("%Y-%m-%d")
            start_time = pst_object.strftime("%H:%M")
            best_of = match["match"]["strategy"]["count"]
            display = match["blockName"]
            if display != title:
                title = display
                output += END + START.format(title, str(best_of), shownname)
            team1 = match["match"]["teams"][0]["name"].strip()
            team2 = match["match"]["teams"][1]["name"].strip()
            games = ""
            for gamen in range(best_of):
                gamen += 1
                games += GAME.format(gamen)
            output += MATCH.format(t1=team1, t2=team2, date=start_date, time=start_time, timezone="PST",
                                   dst=dst, stream=stream, games=games, best_of=str(best_of))

    output = output.replace("{{MatchSchedule/End}}\n\n", "", 1)
    output += END
    return output


def run(league_name, shownname, stream):
    headers = get_headers()
    league_id = get_league(league_name, headers)
    schedule = get_schedules(league_id, headers)
    output = parse_schedule(schedule, shownname, stream)
    return output
