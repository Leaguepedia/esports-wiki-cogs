import asyncio
import logging
import time
import json
from collections import defaultdict
from datetime import datetime
from datetime import timedelta, timezone
from io import BytesIO
from typing import Any, Callable, NoReturn, Optional, TypedDict, Union

import aiohttp
import discord
from dateutil.parser import isoparse
from discord import DMChannel, TextChannel, User
from esports_cog_utils.utils import login_if_possible
from mwrogue.esports_client import EsportsClient
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands import UserInputOptional
from redbot.core.utils.chat_formatting import box, inline, pagify, spoiler
from tsutils.cogs.globaladmin import auth_check, has_perm
from tsutils.helper_functions import repeating_timer
from tsutils.user_interaction import cancellation_message, confirmation_message, get_user_confirmation, \
    send_cancellation_message

from mhtool.errors import NotFoundException
from mhtool.converters import DateConverter

from mhtool.grid_api_wrapper import GridAPIWrapper, FileType, Series, GridFileData


class Game(TypedDict, total=False):
    series: Series
    files: list
    sequence: str
    summary: dict
    details: Union[dict, None]
    platform_game_id: str


logger = logging.getLogger('red.esports-wiki-cogs.mhtool')


async def is_editor(ctx) -> bool:
    GAMHCOG = ctx.bot.get_cog("MHTool")
    return (ctx.author.id in ctx.bot.owner_ids
            or has_perm('mhadmin', ctx.author, ctx.bot)
            or await GAMHCOG.config.user(ctx.author).allowed_tournaments())


async def is_dm_or_whitelisted(ctx) -> bool:
    GAMHCOG = ctx.bot.get_cog("MHTool")
    if not (isinstance(ctx.channel, DMChannel)
            or str(ctx.channel.id) in await GAMHCOG.config.allowed_channels()):
        raise commands.UserFeedbackCheckFailure("This command is only available in"
                                                " DMs or whitelisted channels.")
    return True


class MHTool(commands.Cog):
    def __init__(self, bot: Red, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot

        self.session = aiohttp.ClientSession()
        self.config = Config.get_conf(self, identifier=847356477)
        self.config.register_global(seen={}, allowed_channels={}, autochannel_seen={}, auto_channels={},
                                    invalid_games={}, grid_gte=False)
        self.config.register_user(allowed_tournaments={}, subscriptions={})

        self.api = GridAPIWrapper(bot, self.session)

        self._loop = bot.loop.create_task(self.do_loop())
        self.subscription_lock = asyncio.Lock()

        gadmin: Any = self.bot.get_cog("GlobalAdmin")
        if gadmin:
            gadmin.register_perm('mhadmin')

    async def red_get_data_for_user(self, *, user_id):
        """Get a user's personal data."""
        if subs := await self.config.user_from_id(user_id).subscriptions():
            data = f"You are subscribed to the following tournaments: {', '.join(subs)}"
        else:
            data = "No data is stored for user with ID {}.\n".format(user_id)
        return {"user_data.txt": BytesIO(data.encode())}

    async def red_delete_data_for_user(self, *, requester, user_id):
        """Delete a user's personal data."""
        await self.config.user_from_id(user_id).subscriptions.set({})

    def cog_unload(self):
        self._loop.cancel()
        self.bot.loop.create_task(self.session.close())

    async def do_loop(self) -> NoReturn:
        try:
            async for _ in repeating_timer(120):
                try:
                    series_cache = await self.api.get_series_list(
                        limit=200,
                        gte=(await self.config.grid_gte()) or None,
                        # If we don't specifiy a time limit we get a lot of scheduled games which are useless
                        # We still get some this way but less
                        lte=(
                                datetime.now(timezone.utc) + timedelta(hours=4)
                        ).replace(microsecond=0).astimezone().isoformat(),
                        return_file_list=True,
                        return_parent_tournaments=True
                    )
                    await self.do_auto_channel(series_cache)
                    await self.do_subscriptions(series_cache)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error in loop:")
        except asyncio.CancelledError:
            return

    @staticmethod
    async def get_changed_series(series_cache: list, seen: dict) -> list:
        return sorted(
            (
                series for series in series_cache
                if (series["id"] in seen and len(seen[series["id"]]) or -1) != len(series["file_list"])
            ),
            key=lambda s: isoparse(s['startTimeScheduled'])
        )

    @staticmethod
    async def get_game_file_list(series: Series, game_sequence: str) -> list:
        ret = []

        for file_data in series["file_list"]:
            if file_data["id"].split("-")[-1] == game_sequence:
                ret.append(file_data["id"])

        return ret

    async def extract_games_from_series_list(self, series_list: list[Series],
                                             retrieve_game_summary: Optional[bool] = False,
                                             filt: Optional[Callable] = None, **kwargs) -> list[Game]:
        games = {}

        if filt is None:
            async def filt(*args):
                return True

        for series in series_list:
            for file_data in series["file_list"]:
                game_sequence = file_data["id"].split("-")[-1]
                game_id = f"{series['id']}_{game_sequence}"

                game = {"sequence": game_sequence, "series": series,
                        "files": await self.get_game_file_list(series, game_sequence)}

                if retrieve_game_summary:
                    game["summary"] = await self.api.get_file("summary", series["id"], game_sequence)
                    platform_id = game["summary"]["platformId"]
                    r_game_id = game["summary"]["gameId"]
                    game["platform_game_id"] = f"{platform_id}_{r_game_id}"

                if not await filt(series, file_data, kwargs):
                    continue
                if game_id not in games:
                    games[game_id] = game

        return list(games.values())

    @staticmethod
    async def filter_unseen_files(series: Series, file_data: GridFileData, kwargs):
        seen = kwargs["seen"]
        seen_files = seen.get(series["id"], [])

        if file_data["id"] not in seen_files:
            return True
        return False

    async def do_subscriptions(self, series_cache: list) -> NoReturn:
        async with self.subscription_lock, self.config.seen() as seen:
            tournaments_to_uid = defaultdict(set)
            for u_id, data in (await self.config.all_users()).items():
                for sub in data['subscriptions']:
                    tournaments_to_uid[sub].add(u_id)

            changed_series = await self.get_changed_series(series_cache, seen)

            changed_games = await self.extract_games_from_series_list(changed_series, filt=self.filter_unseen_files,
                                                                      seen=seen)
                    
            for u_id, data in (await self.config.all_users()).items():
                if (user := self.bot.get_user(u_id)) is None:
                    logger.warning(f"Failed to find user with ID {u_id} for subscription.")
                    continue
                msg = [
                    await self.format_game_long(game, user) for game in changed_games if (
                        u_id in tournaments_to_uid[game["series"]["tournament"]["name"]].union(
                            tournaments_to_uid['ALL']
                        )
                        and f"state-details-riot-game-{game['sequence']}" in game["files"]
                        and f"state-summary-riot-game-{game['sequence']}" in game["files"]
                        and await self.has_access(user, game["series"]["tournament"]["name"])
                    )
                ]
                try:
                    for page in pagify('\n\n'.join(msg)):
                        await user.send(page)
                except discord.Forbidden:
                    logger.warning(f"Unable to send subscription message to user {user}. (Forbidden)")

            for series in changed_series:
                seen[series['id']] = [file["id"] for file in series['file_list']]

    async def do_auto_channel(self, series_cache: list) -> NoReturn:
        async with self.config.autochannel_seen() as seen:
            changed_series = await self.get_changed_series(series_cache, seen)

            changed_games = await self.extract_games_from_series_list(changed_series, filt=self.filter_unseen_files,
                                                                      seen=seen)

            msg = [
                await self.format_game_long(game, None) for game in
                changed_games if f"state-details-riot-game-{game['sequence']}" in game["files"]
                and f"state-summary-riot-game-{game['sequence']}" in game["files"]
            ]

            for cid in await self.config.auto_channels():
                if None is not (channel := self.bot.get_channel(int(cid))):
                    for page in pagify('\n\n'.join(msg)):
                        await channel.send(page)

            for series in changed_series:
                seen[series['id']] = [file["id"] for file in series['file_list']]

    @commands.group()
    @commands.check(is_editor)
    @commands.check(is_dm_or_whitelisted)
    async def mhtool(self, ctx):
        """A subcommand for all MHTool commands"""

    @mhtool.group(name='tournament', aliases=['tournaments', 'tag', 'tags'])
    @auth_check('mhadmin')
    async def mh_tournament(self, ctx):
        """Grant adminstration to a specific tournament"""

    @mh_tournament.command(name='add')
    async def mh_t_add(self, ctx, user: discord.User, *, tournament):
        """Add an allowed tournament to a user"""
        if tournament != "ALL":
            tournament = (await self.api.get_parent_tournament(tournament_name=tournament))["name"]
        async with self.config.user(user).allowed_tournaments() as tournaments:
            if tournament not in tournaments:
                tournaments[tournament] = {'date': time.time()}
            else:
                return await ctx.send(f"{user} already has access to `{tournament}`.")
        await ctx.tick()

    @mh_tournament.command(name='remove', aliases=['rm', 'delete', 'del'])
    async def mh_t_remove(self, ctx, user: discord.User, *, tournament=None):
        """Remove an allowed tournament from a user"""
        if tournament is not None and tournament != "ALL":
            tournament = (await self.api.get_parent_tournament(tournament_name=tournament))["name"]
        async with self.config.user(user).allowed_tournaments() as tournaments:
            if tournament is None:
                if await get_user_confirmation(
                        ctx,
                        f"Are you sure you want to remove all allowed tournaments for {user}?"
                ):
                    tournaments.clear()
            elif tournament in tournaments:
                tournaments.pop(tournament)
                async with self.config.user(user).subscriptions() as subs:
                    if tournament in subs:
                        subs.pop(tournament)
            else:
                return await ctx.send(f"{user} already doesn't have access to `{tournament}`.")
        await ctx.tick()

    @mh_tournament.group(name='list')
    async def mh_t_list(self, ctx):
        """Listing subcommand"""

    @mh_t_list.command(name='users', usage='[tournament] [--names]')
    async def mh_t_l_users(self, ctx, *, tournament=None):
        """List all users who are allowed to see a specific tournament, ordered by the date the access was granted

        Leave tournament unfilled to get a list of all users who are able to see any tournament
        """
        users = []
        names = False
        if tournament and tournament.endswith('--names'):
            tournament = tournament[:-len('--names')].strip() or None
            names = True

        if tournament is not None:
            for u_id, data in (await self.config.all_users()).items():
                if tournament in data['allowed_tournaments']:
                    if user := self.bot.get_user(u_id):
                        user = user.name if names else user.mention
                    else:
                        user = f"`{u_id}`"
                    users.append({'user': user, 'date': data['allowed_tournaments'][tournament].get('date', 0)})

            if not users:
                return await ctx.send("No users have been assigned this tournament.")
            users.sort(key=lambda d: d['date'])
            for page in pagify('\n'.join(f"{d['user']}"
                                         f" {datetime.fromtimestamp((d['date'])).strftime('%Y %b %-d')}"
                                         for d in users)):
                await ctx.send(page, allowed_mentions=discord.AllowedMentions(users=False))
        else:
            for u_id, data in (await self.config.all_users()).items():
                if user := self.bot.get_user(u_id):
                    user = user.name if names else user.mention
                else:
                    user = f"`{u_id}`"
                for tournament, tdata in data.get('allowed_tournaments', {}).items():
                    users.append({'user': user, 'tournament': tournament, 'date': tdata.get('date', 0)})

            if not users:
                return await ctx.send("No users have been assigned any tournament.")
            users.sort(key=lambda d: (str(d['user']), d['date']))
            for page in pagify('\n'.join(f"{d['user']} `{d['tournament']}`"
                                         f" {datetime.fromtimestamp((d['date'])).strftime('%Y %b %-d')}"
                                         for d in users)):
                await ctx.send(page, allowed_mentions=discord.AllowedMentions(users=False))

    @mh_t_list.command(name='tournaments', aliases=["tags"])
    async def mh_t_l_tournaments(self, ctx, user: Optional[discord.User] = None, show_names=None):
        """List all the tournaments a specific user is allowed to see

        Leave user unfilled to get a list of all the tournaments any user is allowed to see
        """
        if show_names != "--names":
            show_names = False

        tournaments = []
        if user is not None:
            for tournament, tdata in (await self.config.user(user).allowed_tournaments()).items():
                tournaments.append({'tournament': tournament, 'date': tdata.get('date', 0)})

            if not tournaments:
                return await ctx.send("No tournaments have been assigned to this user.")
            tournaments.sort(key=lambda d: d['date'])
            for page in pagify('\n'.join(f"{inline(d['tournament'])}"
                                         f" {datetime.fromtimestamp((d['date'])).strftime('%Y %b %-d')}"
                                         for d in tournaments)):
                await ctx.send(page, allowed_mentions=discord.AllowedMentions(users=False))
        else:
            for u_id, data in (await self.config.all_users()).items():
                if user := self.bot.get_user(u_id):
                    for tournament, tdata in data.get('allowed_tournaments', {}).items():
                        tournaments.append({'user': user, 'tournament': tournament, 'date': tdata.get('date', 0)})

            if not tournaments:
                return await ctx.send("No tournaments have been assigned to any user.")
            tournaments.sort(key=lambda d: (d['tournament'], d['date']))
            for page in pagify('\n'.join(f"`{d['tournament']}` {d['user'].name if show_names else d['user'].mention}"
                                         f" {datetime.fromtimestamp((d['date'])).strftime('%Y %b %-d')}"
                                         for d in tournaments)):
                await ctx.send(page, allowed_mentions=discord.AllowedMentions(users=False))

    @mh_t_list.command(name='all')
    async def mh_t_l_all(self, ctx):
        """List all available tournaments sorted alphabetically by length"""
        for page in pagify(
                ', '.join(map(inline, sorted(
                    ["ALL"] + [tournament["name"] for tournament in
                               await self.api.get_tournaments_list(has_parent=False)]
                ))), delims=[', ']
        ):
            await ctx.send(page.strip(', '))

    @mh_t_list.command(name='inuse', aliases=['used'])
    async def mh_t_l_inuse(self, ctx):
        """List all in-use tournaments"""
        tournaments = set()
        for user, data in (await self.config.all_users()).items():
            tournaments.update(set(data.get('allowed_tournaments', {}).keys()))
            tournaments.update(set(data.get('subscriptions', {}).keys()))
        if not tournaments:
            return await ctx.send("There are no in use tournaments.")
        for page in pagify(', '.join(map(inline, sorted(tournaments))), delims=[', ']):
            await ctx.send(page.strip(', '))

    @mh_t_list.command(name='invalid')
    async def mh_t_l_invalid(self, ctx):
        """List all currently invalid tournaments"""
        tournaments = set()
        for user, data in (await self.config.all_users()).items():
            tournaments.update(set(data.get('allowed_tournaments', {})))
            tournaments.update(set(data.get('subscriptions', {})))
        tournaments.difference_update(
            ["ALL"] + [tournament["name"] for tournament in await self.api.get_tournaments_list(has_parent=False)]
        )
        if not tournaments:
            return await ctx.send("There are no invalid tournaments.")
        for page in pagify(', '.join(map(inline, sorted(tournaments))), delims=[', ']):
            await ctx.send(page.strip(', '))

    @mhtool.group(name='query')
    async def mh_query(self, ctx):
        """Slow query commands"""

    @mh_query.command(name='all')
    async def mh_q_all(self, ctx, limit: UserInputOptional[int] = 50, *, tournament):
        """Get a list of the most recent `limit` games with the provided tournament

        If limit is left blank, 50 games are sent.
        """
        if not await self.has_access(ctx.author, tournament):
            return await ctx.send(f"You do not have permission to query the tournament `{tournament}`.")

        series_list = sorted(
            await self.api.get_series_list(tournament_name=tournament,
                                           return_parent_tournaments=True,
                                           return_file_list=True,
                                           include_tournament_children=True),
            key=lambda s: isoparse(s['startTimeScheduled'])
        )

        games = await self.extract_games_from_series_list(series_list)

        ret = [await self.format_game_long(game, ctx.author) for game in games[-limit:]]
        if not ret:
            return await ctx.send(f"There are no games with tournament `{tournament}`."
                                  f" Make sure the tournament is valid and correctly cased.")
        for page in pagify('\n\n'.join(ret), delims=['\n\n']):
            await ctx.send(page)

    @mh_query.command(name='new')
    async def mh_q_new(self, ctx, limit: UserInputOptional[int] = 50, *, tournament):
        """Get only games that aren't on the wiki yet"""
        if not await self.has_access(ctx.author, tournament):
            return await ctx.send(f"You do not have permission to query the tournament `{tournament}`.")

        site = await login_if_possible(ctx, self.bot, 'lol')

        series_list = sorted(
            await self.api.get_series_list(tournament_name=tournament,
                                           return_parent_tournaments=True,
                                           return_file_list=True,
                                           include_tournament_children=True),
            key=lambda s: isoparse(s['startTimeScheduled'])
        )

        if not series_list:
            return await ctx.send(f"There are no games with tournament `{tournament}`."
                                  f" Make sure the tournament is valid and correctly cased.")

        games = (await self.filter_new(
            site,
            await self.extract_games_from_series_list(series_list, retrieve_game_summary=True)
        ))[-limit:]

        ret = [await self.format_game_long(game, ctx.author) for game in games]

        if not ret:
            return await ctx.send(f"There are no new games with tournament `{tournament}`.")

        for page in pagify('\n\n'.join(ret), delims=['\n\n']):
            await ctx.send(page)

    @mh_query.command(name='since')
    async def mh_q_since(self, ctx, date: DateConverter, limit: UserInputOptional[int] = 50, *, tournament):
        """Get only games since a specific date

        The results are filtered using the scheduled start time for the series corresponding to each game"""
        if not await self.has_access(ctx.author, tournament):
            return await ctx.send(f"You do not have permission to query the tournament `{tournament}`.")

        series_list = sorted(
            await self.api.get_series_list(tournament_name=tournament,
                                           return_parent_tournaments=True,
                                           return_file_list=True,
                                           include_tournament_children=True,
                                           gte=date),
            key=lambda s: isoparse(s['startTimeScheduled'])
        )

        if not series_list:
            return await ctx.send(f"There are no games with tournament `{tournament}` after the given date."
                                  f" Make sure the tournament is valid and correctly cased.")

        games = (await self.extract_games_from_series_list(series_list))[-limit:]

        ret = [await self.format_game_long(game, ctx.author) for game in games]

        if not ret:
            return await ctx.send(f"There are no new games with tournament `{tournament}`.")

        for page in pagify('\n\n'.join(ret), delims=['\n\n']):
            await ctx.send(page)

    @mh_query.command(name='getgame')
    async def mh_q_getgame(self, ctx, game_id):
        """Get a game by its game ID"""
        game_id = game_id.strip()
        try:
            series_data = await self.api.get_series_data_by_platform_game_id(game_id, return_parent_tournament=True)
            series_files = await self.api.get_files_by_platform_game_id(game_id)
        except NotFoundException:
            return await ctx.send("The game could not be found!")
        tournament_name = series_data["tournament"]["name"]
        if not await self.has_access(ctx.author, tournament_name):
            return await ctx.send(f'You do not have permission to query the tournament `{tournament_name}`.')
        await ctx.send(await self.format_game_long(
            {
                "series": series_data,
                "platform_game_id": game_id,
                "summary": series_files[0],
                "details": series_files[1]
            },
            ctx.author
        ))

    @mh_query.command(name='findgame')
    async def mh_q_findgame(self, ctx, game_id):
        """Finds the tournament and game in the wiki corresponding to the given game ID"""
        site = await login_if_possible(ctx, self.bot, 'lol')

        result = site.cargo_client.query(
            tables="MatchScheduleGame=MSG, Tournaments=T",
            fields="MSG.Blue, MSG.Red, MSG.Winner, T.StandardName, MSG._pageName=Page, MSG.GameId",
            join_on="MSG.OverviewPage=T.OverviewPage",
            where=f"MSG.RiotPlatformGameId = '{game_id}'"
        )

        if not result:
            await ctx.send("The given ID could not be found on the wiki!")

        for item in result:
            team1, team2 = item["Blue"] or "Unknown", item["Red"] or "Unknown"
            winner = "None"
            if item["Winner"] == "1":
                winner = team1
            elif item["Winner"] == "2":
                winner = team2
            await ctx.send(f"`{game_id}`\n"
                           f"\t\tPage: `{item['Page']}`\n"
                           f"\t\tTournament: `{item['StandardName']}`\n"
                           f"\t\tWiki Game ID: `{item['GameId']}`\n"
                           f"\t\tTeams: `{team1}` vs `{team2}`\n"
                           f"\t\tWinner: `{winner}`")

    @mh_query.command(name='getasset')
    @auth_check('mhadmin')
    async def mh_q_getasset(self, ctx, game_id, file_type: FileType):
        """Get a match file by platform game id and file type, file type must be either summary or details"""
        await ctx.send(file=discord.File(BytesIO(json.dumps(
            await self.api.get_one_file_by_platform_game_id(game_id, file_type)
        ).encode("utf-8")), f'{game_id}_{file_type}.json'))

    @mhtool.group(name='subscription', aliases=['subscriptions', 'subscribe'])
    async def mh_subscription(self, ctx):
        """Subscribe to a tournament"""

    @mh_subscription.command(name='add')
    async def mh_s_add(self, ctx, *, tournament):
        """Subscribe to a tournament"""
        tournament = (await self.api.get_parent_tournament(tournament_name=tournament))["name"]
        async with self.config.user(ctx.author).subscriptions() as subs:
            if tournament in subs:
                return await ctx.send("You're already subscribed to that tournament.")
            if not await self.has_access(ctx.author, tournament):
                return await send_cancellation_message(ctx, f"You cannot subscribe to tournament `{tournament}` as"
                                                            f" you don't have permission to view it."
                                                            f" Contact a bot admin if you think this is an issue.")
            subs[tournament] = {'date': time.time(), 'spoiler': False}
        await ctx.tick()

    @mh_subscription.command(name='remove', aliases=['rm', 'delete', 'del'])
    async def mh_s_remove(self, ctx, *, tournament):
        """Unsubscribe from a tournament"""
        tournament = (await self.api.get_parent_tournament(tournament_name=tournament))["name"]
        async with self.config.user(ctx.author).subscriptions() as subs:
            if tournament not in subs:
                return await ctx.send("You're not subscribed to that tournament.")
            subs.pop(tournament)
        await ctx.tick()

    @mh_subscription.command(name='list')
    async def mh_s_list(self, ctx):
        """List your subscribed tournaments"""
        subs = await self.config.user(ctx.author).subscriptions()
        if not subs:
            return await ctx.send("You are not subscribed to any tournaments.")
        await ctx.send(f"You are subscribed to the following tournaments: {', '.join(map(inline, subs))}")

    @mh_subscription.command(name='clear', aliases=['purge'])
    async def mh_s_clear(self, ctx):
        """Clear your current subscriptions"""
        if not await get_user_confirmation(ctx, "Are you sure you want to clear all of your subscriptions?"):
            return await ctx.react_quietly("\N{CROSS MARK}")
        await self.config.user(ctx.author).subscriptions.set({})
        await ctx.tick()

    @mh_subscription.group(name='set')
    async def mh_s_set(self, ctx):
        """Change settings about your subscriptions"""

    @mh_s_set.command(name='spoiler')
    async def mh_s_s_spoiler(self, ctx, *, tournament):
        async with self.config.user(ctx.author).subscriptions() as subs:
            if tournament not in subs:
                return await ctx.send("You're not subscribed to that tournament.")
            subs[tournament]['spoiler'] = True
        await ctx.tick()

    @mh_s_set.command(name='unspoiler')
    async def mh_s_s_unspoiler(self, ctx, *, tournament):
        async with self.config.user(ctx.author).subscriptions() as subs:
            if tournament not in subs:
                return await ctx.send("You're not subscribed to that tournament.")
            subs[tournament]['spoiler'] = False
        await ctx.tick()

    @mh_subscription.group(name='settings')
    async def mh_s_settings(self, ctx, *, tournament):
        """Show the settings of a subscription"""
        subs = await self.config.user(ctx.author).subscriptions()
        if tournament not in subs:
            return await ctx.send("You're not subscribed to that tournament.")
        sub = subs[tournament]
        await ctx.send(f"Tournament: {tournament}\n"
                       f"Spoiler: {sub['spoiler']}")

    @mhtool.group(name='channels', aliases=['channel'])
    @auth_check('mhadmin')
    async def mh_channels(self, ctx):
        """Set whitelisted channels for the use of this cog"""

    @mh_channels.command(name="add")
    async def mh_c_add(self, ctx, channel: TextChannel):
        """Add a channel"""
        async with self.config.allowed_channels() as channels:
            channels[str(channel.id)] = {'date': time.time()}
        await ctx.tick()

    @mh_channels.command(name='remove', aliases=['rm', 'delete', 'del'])
    async def mh_c_remove(self, ctx, channel: TextChannel):
        """Remove a channel"""
        async with self.config.allowed_channels() as channels:
            if str(channel.id) in channels:
                channels.pop(str(channel.id))
            else:
                return await ctx.send(f"{channel} was not already an allowed channel.")
        await ctx.tick()

    @mh_channels.command(name="list")
    async def mh_c_list(self, ctx):
        """List whitelisted channels"""
        channels = [channel for cid in await self.config.allowed_channels()
                    if (channel := self.bot.get_channel(int(cid)))]
        if not channels:
            return await ctx.send("There are no whitelisted channels.")
        for page in pagify('\n'.join(f"{c.id} ({c.guild.name}/{c.name})" for c in channels)):
            await ctx.send(box(page))

    @mhtool.group(name='autochannels', aliases=['autochannel'])
    @auth_check('mhadmin')
    async def mh_autochannels(self, ctx):
        """Set channels to get all games sent to"""

    @mh_autochannels.command(name="add")
    async def mh_ac_add(self, ctx, channel: TextChannel):
        """Add a channel"""
        async with self.config.auto_channels() as channels:
            channels[str(channel.id)] = {'date': time.time()}
        await ctx.tick()

    @mh_autochannels.command(name='remove', aliases=['rm', 'delete', 'del'])
    async def mh_ac_remove(self, ctx, channel: TextChannel):
        """Remove a channel"""
        async with self.config.auto_channels() as channels:
            if str(channel.id) in channels:
                channels.pop(str(channel.id))
            else:
                return await ctx.send(f"{channel} was not already an auto channel.")
        await ctx.tick()

    @mh_autochannels.command(name="list")
    async def mh_ac_list(self, ctx):
        """List auto-channels"""
        channels = [channel for cid in await self.config.auto_channels()
                    if (channel := self.bot.get_channel(int(cid)))]
        if not channels:
            return await ctx.send("There are no auto channels.")
        for page in pagify('\n'.join(f"{c.id} ({c.guild.name}/{c.name})" for c in channels)):
            await ctx.send(box(page))

    @mhtool.group(name='gset')
    @auth_check('mhadmin')
    async def mh_gset(self, ctx):
        """Set global settings for mhtool"""

    @mh_gset.command(name="startdate")
    async def mh_gset_sd(self, ctx, *, date: str):
        """Set the beginning date of the time range in which we are looking for new series

        The date must be ISO formatted (yyyy-mm-ddThh:mm:ss-07:00)"""
        await self.config.grid_gte.set(str(date.strip()))
        await ctx.tick()

    async def format_game_long(self, game: Game, user: Optional[User]) -> str:
        tournament = game["series"]["tournament"]["name"]

        subs = {} if user is None else await self.config.user(user).subscriptions()
        use_spoiler_tags = subs.get(tournament, {}).get("spoiler")
        use_spoiler_tags = use_spoiler_tags or subs.get('ALL', {}).get('spoiler')

        teams = winner = 'Unknown'
        has_winner = False

        summary = (game.get("summary") or
                   await self.api.get_file("summary", game["series"]["id"], game["sequence"]))
        platform_game_id = f"{summary['platformId']}_{summary['gameId']}"
        if "participants" in summary and len(summary['participants'][::5]) == 2:
            t1, t2 = summary['participants'][::5]
            team1_short = (t1.get('riotIdGameName') or t1['summonerName']).split(' ')[0]
            team2_short = (t2.get('riotIdGameName') or t2['summonerName']).split(' ')[0]
            if t1["win"]:
                winner = team1_short
                has_winner = True
                if not use_spoiler_tags:
                    team1_short = f"**{team1_short}**"
            elif t2["win"]:
                winner = team2_short
                has_winner = True
                if not use_spoiler_tags:
                    team2_short = f"**{team2_short}**"
            else:
                winner = "None"
            teams = f"{team1_short} vs {team2_short}"
            if use_spoiler_tags:
                winner = spoiler(winner.ljust(30))
        ready_to_parse_string = self.get_ready_to_parse_string(game, has_winner)
        return (f"`{platform_game_id}`{ready_to_parse_string}\n"
                f"\t\tSeries ID: `{game['series']['id']}`\n"
                f"\t\tName: {summary['gameName']}\n"
                f"\t\tTeams: {teams}\n"
                f"\t\tWinner: {winner}\n"
                f"\t\tStart Time: {self.format_timestamp(round(summary['gameCreation']/1000))}\n"
                f"\t\tTournament: `{tournament}`")

    @staticmethod
    def get_ready_to_parse_string(game: Game, has_winner: bool = True):
        if not has_winner:
            return cancellation_message("Not ready to parse")
        if game.get("files") and f"state-details-riot-game-{game['sequence']}" not in game["files"]:
            return confirmation_message("Ready to parse, but no drakes (Possible chronobreak. Please check back later)")
        if not game.get("files") and not game.get("details"):
            return confirmation_message("Ready to parse, but no drakes (Possible chronobreak. Please check back later)")
        return confirmation_message("Ready to parse")

    @staticmethod
    def format_timestamp(timestamp: int) -> str:
        return f"<t:{timestamp}:F>"

    @staticmethod
    async def filter_new(site: EsportsClient, games: list[Game]) -> list:
        """Returns only new games from a list of games."""
        if not games:
            return []

        all_ids = [repr(game['platform_game_id'].strip()) for game in games]
        where = f"RiotPlatformGameId IN ({','.join(all_ids)}) AND HasRpgidInput = '1'"

        result = site.cargo_client.query(tables="MatchScheduleGame",
                                         fields="RiotPlatformGameId",
                                         where=where)

        old_ids = [row['RiotPlatformGameId'] for row in result]
        return [game for game in games if game['platform_game_id'] not in old_ids]

    async def has_access(self, user, tournament):
        if has_perm('mhadmin', user, self.bot) or user.id in self.bot.owner_ids:
            return True
        for utournament in await self.config.user(user).allowed_tournaments():
            if utournament == "ALL":
                return True
            if utournament == tournament:
                return True
        return False
