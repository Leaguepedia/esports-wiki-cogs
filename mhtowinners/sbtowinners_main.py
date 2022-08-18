import mwparserfromhell
from mwcleric.errors import RetriedLoginAndStillFailed
from mwrogue.auth_credentials import AuthCredentials
from mwrogue.esports_client import EsportsClient


class SbToWinnersRunner:
    summary = "Discover sides & winners from the SB & populate in the row"

    def __init__(self, site: EsportsClient, title_list):
        self.site = site
        self.events_to_skip = []
        if title_list != [""]:
            self.title_list = [f"\"{site.cache.get_target(title)}\"" for title in title_list]
        else:
            self.title_list = []

    def get_where(self):
        where = f"(MSG.Blue IS NULL OR MSG.Red IS NULL OR MSG.Winner IS NULL) AND (SG.Team1 IS NOT NULL OR " \
                f"SG.Team2 IS NOT NULL OR SG.WinTeam IS NOT NULL) " \
                f"AND MSG.OverviewPage NOT IN ({','.join(self.events_to_skip)}) "
        if self.title_list:
            where += f"AND MSG.OverviewPage IN ({','.join(self.title_list)})"
        print(where)
        return where

    def run(self):
        result = self.site.cargo_client.query(
            tables="TournamentScriptsToSkip",
            fields="OverviewPage",
            where='Script="sbtowinners"'
        )

        for item in result:
            self.events_to_skip.append("'{}'".format(item["OverviewPage"]))

        fields = [
            "SG.Team1",
            "SG.Team2",
            "SG.WinTeam",
            "SG.MatchHistory",
            "MSG.N_MatchInTab",
            "MSG.N_TabInPage",
            "MSG.N_GameInMatch",
            "MSG._pageName=DataPage",
        ]
        result = self.site.cargo_client.query(
            tables="ScoreboardGames=SG, MatchScheduleGame=MSG",
            fields=fields,
            join_on="MSG.GameId=SG.GameId",
            where=self.get_where(),
            order_by='MSG._pageName'
        )

        current_page = {
            'page': None,
            'wikitext': None,
            'page_name': None,
            'old_text': None,
        }

        for item in result:
            tab_target = int(item['N TabInPage'])
            match_target = int(item['N MatchInTab'])
            game_target = int(item['N GameInMatch'])

            if current_page['page_name'] != item['DataPage']:
                if current_page['page'] is not None:
                    self.save_page(current_page)
                current_page['page_name'] = item['DataPage']
                current_page['page'] = self.site.client.pages[current_page['page_name']]
                old_text = current_page['page'].text()
                current_page['old_text'] = old_text
                current_page['wikitext'] = mwparserfromhell.parse(old_text)

            tab_counter = 0
            match_counter = 0
            game_counter = 0
            for template in current_page['wikitext'].filter_templates():
                if template.name.matches("MatchSchedule/Start"):
                    tab_counter += 1
                    match_counter = 0
                elif template.name.matches("MatchSchedule"):
                    match_counter += 1
                    game_counter = 0
                elif template.name.matches("MatchSchedule/Game"):
                    game_counter += 1
                    if (tab_counter, match_counter, game_counter) == (tab_target, match_target, game_target):
                        if not template.has("blue", ignore_empty=True):
                            template.add("blue", item['Team1'])
                        if not template.has("red", ignore_empty=True):
                            template.add("red", item['Team2'])
                        if not template.has("winner", ignore_empty=True):
                            template.add("winner", item['WinTeam'])

        # we need to catch the last iteration too (assuming we actually did anything)
        if current_page['page'] is not None:
            self.save_page(current_page)

    def save_page(self, page_dict):
        new_text = str(page_dict['wikitext'])
        if new_text != page_dict['old_text']:
            try:
                self.site.save(page_dict['page'], new_text, summary=self.summary)
            except RetriedLoginAndStillFailed:
                pass


if __name__ == '__main__':
    credentials = AuthCredentials(user_file='me')
    lol_site = EsportsClient('lol', credentials=credentials)  # Set wiki
    SbToWinnersRunner(lol_site, [""]).run()
