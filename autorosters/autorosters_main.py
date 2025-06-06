from typing import Optional

from esports_cog_utils.task_runner import TaskRunner
from mwrogue.esports_client import EsportsClient
from mwrogue.auth_credentials import AuthCredentials
import math
import re


class AutoRostersRunner(TaskRunner):
    PAGE_TABS = "{{{{Tabs:{}}}}}"
    PAGE_HEADER = "{{TOCFlat}}"
    TEAM_TEXT = "\n\n==={{{{team|{}}}}}===\n{{{{ExtendedRoster{}{}\n}}}}"
    PLAYER_TEXT = "\n|{{{{ExtendedRoster/Line{}{}\n{} }}}}"

    role_numbers = {
        "Top": 1,
        "Jungle": 2,
        "Mid": 3,
        "Bot": 4,
        "Support": 5,
        "Coach": 6
    }

    def __init__(self, site: EsportsClient, overview_page: str, query_coaches: bool = False):
        super().__init__()
        self.site = site
        self.overview_page = overview_page
        self.tabs: Optional[str] = None
        self.match_data = {}
        self.alt_teamnames = {}
        self.rosters_data = {}
        self.coaches = {}
        self.query_coaches = query_coaches

    def run(self):
        self.get_tabs()
        matchschedule_data = self.query_matchschedule_data()
        scoreboard_data = self.query_scoreboard_data(matchschedule_data)
        self.process_matchschedule_data(matchschedule_data)
        self.process_scoreboard_data(scoreboard_data)
        self.query_tournament_coaches()
        self.initialize_roster_data()
        self.add_coaches_to_roster_data()
        players_data = self.get_player_data()
        self.process_game_data()
        output = self.make_output(players_data)
        self.save_page(output)

    def get_tabs(self):
        page = self.site.client.pages[self.overview_page]
        page_text = page.text()
        tabs = re.search(r'{{Tabs:(.*?)}}', page_text)
        self.tabs = tabs[1] if tabs else None

    def query_matchschedule_data(self):
        matchschedule_data = self.site.cargo_client.query(
            tables="MatchSchedule=MS, MatchScheduleGame=MSG",
            fields=["MS.MatchId", "MSG.GameId", "MS.FF=MSFF", "MSG.FF=MSGFF", "MS.BestOf", "MS.Team1Final",
                    "MS.Team2Final", "MS.Team1", "MS.Team2", "MS.Winner=MatchWinner"],
            join_on="MS.MatchId=MSG.MatchId",
            where=f"MS.OverviewPage = '{self.overview_page}' AND MS.Team1 != \"TBD\" AND MS.Team2 != \"TBD\"",
            order_by="MS.N_Page, MS.N_MatchInPage, MSG.N_GameInMatch"
        )
        return matchschedule_data

    @staticmethod
    def get_where_scoreboard_data(matchschedule_data):
        where = "SG.GameId IN ({})"
        gameids_to_query = []
        for game in matchschedule_data:
            if game["MSFF"] or game["MSGFF"]:
                continue
            if not game["MatchWinner"]:
                continue
            gameids_to_query.append(f"\"{game['GameId']}\"")
        where = where.format(" ,".join(gameids_to_query))
        return where

    def query_scoreboard_data(self, matchschedule_data):
        where = self.get_where_scoreboard_data(matchschedule_data)
        scoreboard_data = self.site.cargo_client.query(
            tables="ScoreboardGames=SG, ScoreboardPlayers=SP",
            fields=["SG.OverviewPage", "SG.Team1", "SG.Team2", "SP.IngameRole", "SP.Team", "SP.Link", "SG.GameId",
                    "SG.MatchId"],
            order_by="SG.N_Page, SG.N_MatchInPage, SG.N_GameInMatch",
            where=where,
            join_on="SG.GameId=SP.GameId"
        )
        return scoreboard_data

    def process_matchschedule_data(self, matchschedule_data):
        for match in matchschedule_data:
            if not self.match_data.get(match["MatchId"]):
                self.match_data[match["MatchId"]] = {"ff": False, "best_of": match["BestOf"], "team1": match["Team1"],
                                                     "team2": match["Team2"], "games": {}}
                if match["MSFF"]:
                    self.match_data[match["MatchId"]]["ff"] = True
                self.alt_teamnames[match["Team1"]] = match["Team1Final"]
                self.alt_teamnames[match["Team2"]] = match["Team2Final"]
            self.match_data[match["MatchId"]]["games"][match["GameId"]] = {"msg_data": match}

    def get_player_id(self, player):
        response = self.site.cargo_client.query(
            tables="Players=P, PlayerRedirects=PR",
            fields="P.Player",
            where=f"PR.AllName = \"{player}\"",
            join_on="P.OverviewPage=PR.OverviewPage"
        )
        if not response:
            return player
        return response[0]["Player"]

    def process_scoreboard_data(self, scoreboard_data):
        player_ids_cache = {}

        for scoreboard in scoreboard_data:
            game_data = self.match_data[scoreboard["MatchId"]]["games"][scoreboard["GameId"]]
            if "sg_data" not in game_data.keys():
                game_data["sg_data"] = {
                    "team1": scoreboard["Team1"],
                    "team2": scoreboard["Team2"],
                    "players": {}}
            if scoreboard["Link"] not in player_ids_cache:
                player_id = self.get_player_id(scoreboard["Link"])
                player_ids_cache[scoreboard["Link"]] = player_id
            player_page = player_ids_cache[scoreboard["Link"]]
            game_data["sg_data"]["players"][player_page] = {"role": scoreboard["IngameRole"],
                                                            "team": scoreboard["Team"],
                                                            "link": player_page}

    def query_tournament_coaches(self):
        if not self.query_coaches:
            return
        tournament_coaches = self.site.cargo_client.query(
            tables="TournamentPlayers=TP",
            fields="TP.Player, TP.Team",
            where=f"TP.OverviewPage = '{self.overview_page}' AND TP.Role = 'Coach' AND TP.Player IS NOT NULL",
            order_by="TP.N_PlayerInTeam ASC"
        )
        self.coaches = [{"link": coach["Player"], "team": coach["Team"]} for coach in tournament_coaches]

    def get_players_roles_data(self):
        for team, team_data in self.rosters_data.items():
            for player, player_data in team_data["players"].items():
                player = self.rosters_data[team]["players"][player]
                rolesn = len(player_data["roles"])
                player["roles_data"]["roles"] = rolesn
                for i, role in enumerate(player["roles"]):
                    rolen = f"role{i + 1}"
                    rolen_short = f"r{i + 1}"
                    player["roles_data"][rolen] = role
                    player["games_by_role"][rolen_short] = ""

    def initialize_roster_data(self):
        for match in self.match_data.values():
            for team in (match["team1"], match["team2"]):
                team = self.alt_teamnames[team]
                if team not in self.rosters_data:
                    self.rosters_data[team] = {"players": {}, "teamsvs": []}
            for game in match["games"].values():
                if game.get("sg_data"):
                    for player in game["sg_data"]["players"].values():
                        team = self.alt_teamnames[player["team"]]
                        team_players = self.rosters_data[team]["players"]
                        if player["link"] not in team_players.keys():
                            team_players[player["link"]] = {"roles": [], "roles_data": {},
                                                            "games_by_role": {}}
                        if player["role"] not in team_players[player["link"]]["roles"]:
                            team_players[player["link"]]["roles"].append(player["role"])
        self.get_players_roles_data()

    def add_coaches_to_roster_data(self):
        for coach in self.coaches:
            team = self.alt_teamnames[coach["team"]]
            self.rosters_data[team]["players"][coach["link"]] = {"roles": ["Coach"],
                                                                 "roles_data": {"roles": 1, "role1": "Coach"},
                                                                 "games_by_role": {}}

    @staticmethod
    def get_where_player_data(rosters_data):
        where = "PR.AllName IN ({})"

        players = {}
        for team in rosters_data.values():
            for player in team["players"].keys():
                if player not in players.keys():
                    players[player] = f"\"{player}\""
        where = where.format(" ,".join(players.values()))
        return where

    def get_player_data(self):
        players_data = {}

        where = self.get_where_player_data(self.rosters_data)
        response = self.site.cargo_client.query(
            tables="Players=P, PlayerRedirects=PR, Alphabets=A",
            join_on="P.OverviewPage=PR.OverviewPage, P.NameAlphabet=A.Alphabet",
            where=where,
            fields=["CONCAT(CASE WHEN A.IsTransliterated=\"1\" THEN P.NameFull ELSE P.Name END)=name", "P.Player",
                    "P.NationalityPrimary=NP", "P.Country", "P.Residency"]
        )

        for player_data in response:
            player_name = player_data["name"].replace("&amp;nbsp;", " ") if player_data["name"] is not None else ""
            players_data[player_data["Player"].capitalize()] = [{"flag": player_data["NP"] or
                                                                player_data["Country"] or ""},
                                                                {"res": player_data["Residency"]} or "",
                                                                {"player": player_data["Player"]},
                                                                {"name": player_name}]
        return players_data

    def add_team_vs(self, current_teams):
        n_teams = {}
        for team in current_teams:
            if team not in self.rosters_data.keys():
                self.rosters_data[team] = {"teamsvs": []}
            if not self.rosters_data[team]["teamsvs"]:
                n_teams[team] = 0
            n_teams[team] = len(self.rosters_data[team]["teamsvs"]) + 1
        self.rosters_data[current_teams[0]]["teamsvs"].append({f"team{n_teams[current_teams[0]]}": current_teams[1]})
        self.rosters_data[current_teams[1]]["teamsvs"].append({f"team{n_teams[current_teams[1]]}": current_teams[0]})

    def process_game_data(self):
        for match in self.match_data.values():
            current_teams = [self.alt_teamnames[match["team1"]], self.alt_teamnames[match["team2"]]]
            self.add_team_vs(current_teams)
            if match["ff"]:
                for team in current_teams:
                    if "players" not in self.rosters_data[team].keys():
                        continue
                    for player in self.rosters_data[team]["players"].values():
                        for role in player["games_by_role"].keys():
                            player["games_by_role"][role] += f"{'n' * math.ceil((int(match['best_of']) + 1) / 2)},"
                continue
            for game in match["games"].values():
                if game["msg_data"]["MSGFF"] is not None:
                    for team in current_teams:
                        if "players" not in self.rosters_data[team].keys():
                            continue
                        for player in self.rosters_data[team]["players"].values():
                            for role in player["games_by_role"].keys():
                                player["games_by_role"][role] += "n"
                    continue
                for team in current_teams:
                    rd_team = self.rosters_data[team]
                    for player in rd_team["players"].keys():
                        game_rd_player = rd_team["players"][player]
                        if "sg_data" in game.keys():
                            game_sg_players = game["sg_data"]["players"]
                            if player in game_sg_players.keys():
                                game_sg_player = game_sg_players[player]
                                if team == self.alt_teamnames[game_sg_player["team"]]:
                                    for role in game_rd_player["games_by_role"]:
                                        lookup_role = role.replace("r", "role")
                                        role_name = game_rd_player["roles_data"][lookup_role]
                                        if role_name == game_sg_player["role"]:
                                            game_rd_player["games_by_role"][role] += "y"
                                        else:
                                            game_rd_player["games_by_role"][role] += "n"
                                    continue
                            for role in self.rosters_data[team]["players"][player]["games_by_role"]:
                                game_rd_player["games_by_role"][role] += "n"
            for team in current_teams:
                rd_team = self.rosters_data[team]
                for player in rd_team["players"].keys():
                    game_rd_player = rd_team["players"][player]
                    for role in game_rd_player["games_by_role"]:
                        game_rd_player["games_by_role"][role] += ","
        for team_data in self.rosters_data.values():
            if "players" not in team_data.keys():
                continue
            for player in team_data["players"].values():
                for role, role_data in player["games_by_role"].items():
                    player["games_by_role"][role] = role_data.rstrip(",")

    def get_order(self):
        sorted_teams = sorted(self.rosters_data.keys(), key=lambda x: x.lower())
        sorted_data = {"teams": sorted_teams, "players": {}}
        for team, team_data in self.rosters_data.items():
            team_players = {}
            if "players" not in team_data.keys():
                sorted_data["players"][team] = {}
                continue
            for player, player_data in team_data["players"].items():
                team_players[player] = self.role_numbers[player_data["roles"][0]]
            sorted_data["players"][team] = sorted(team_players.items(), key=lambda x: x[1])
        return sorted_data

    @staticmethod
    def concat_args(data):
        ret = ''
        lookup = data
        if type(data) == dict:
            lookup = []
            for k, v in data.items():
                lookup.append({k: v})

        for pair in lookup:
            pair: dict
            for key in pair.keys():
                if pair[key] is None:
                    ret = ret + '|{}='.format(key)
                else:
                    ret = ret + '|{}={}'.format(key, str(pair[key]))
        return ret

    def make_output(self, players_data):
        output = ""
        if self.tabs:
            output += self.PAGE_TABS.format(self.tabs)
        else:
            self.warnings.append("There are no tabs on the overview page!")
        output += self.PAGE_HEADER
        sorted_data = self.get_order()
        for team in sorted_data["teams"]:
            players_text = ""
            if not sorted_data["players"][team]:
                continue
            team_has_ingame_players = False
            for player_data in self.rosters_data[team]["players"].values():
                if all(role == "Coach" for role in player_data["roles"]):
                    continue
                team_has_ingame_players = True
            if not team_has_ingame_players:
                continue
            for player in sorted_data["players"][team]:
                player = player[0]
                game_rd_player = self.rosters_data[team]["players"][player]
                if players_data.get(player.capitalize()):
                    player_data = self.concat_args(players_data[player.capitalize()])
                else:
                    player_data = self.concat_args([{"flag": ""}, {"res": ""}, {"player": player}, {"name": ""}])
                player_roles_data = self.concat_args(game_rd_player["roles_data"])
                player_games_by_role = self.concat_args(game_rd_player["games_by_role"])
                if not game_rd_player["games_by_role"]:
                    player_games_by_role = "|r1="
                players_text += self.PLAYER_TEXT.format(player_data, player_roles_data, player_games_by_role)
            teamsvs = self.concat_args(self.rosters_data[team]["teamsvs"])
            output += self.TEAM_TEXT.format(team, teamsvs, players_text)
        return output

    def save_page(self, output):
        username = self.site.credentials.username
        username = username.split('@')[0] if "@" in username else username
        page = self.site.client.pages[f"User:{username}/Team Rosters Sandbox"]
        self.site.save(page=page, text=output, summary="Generating Rosters from Scoreboard Data")


if __name__ == '__main__':
    credentials = AuthCredentials(user_file='botcov')
    lol_site = EsportsClient('lol', credentials=credentials)
    AutoRostersRunner(lol_site, "Liga Nexo/2025 Season/Split 2").run()
