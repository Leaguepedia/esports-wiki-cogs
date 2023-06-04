import mwparserfromhell
from mwcleric.errors import RetriedLoginAndStillFailed
from mwrogue.esports_client import EsportsClient
from lol_dto.classes.game import LolGameTeam

import riot_transmute
from bayes_lol_client import BayesEMH


def tl_has(tl, param):
    return tl.has(param) and tl.get(param).value.strip() != ''


class MhToWinnersRunner(object):
    def __init__(self, site: EsportsClient, title_list: list):
        self.site = site
        self.summary = 'Discover sides & winners from the MH & populate in the row'
        self.emh = BayesEMH()
        self.title_list = [f'"{self.site.cache.get_target(title)}"' for title in title_list]

    def run(self):
        pages_to_edit = self.site.cargo_client.query(
            tables="MatchSchedule=MS",
            fields="MS._pageName=Page, MS.OverviewPage",
            where=f"MS.OverviewPage IN ({','.join(self.title_list)})",
            group_by="MS._pageName"
        )
        self.update_pages(pages_to_edit)
    
    def update_pages(self, pages_to_edit):
        for item in pages_to_edit:
            page = self.site.client.pages[item['Page']]
            text = page.text()
            wikitext = mwparserfromhell.parse(text)
            self.update_wikitext(wikitext, item['OverviewPage'])
            self.site.report_all_errors('mhtowinners')
            new_text = str(wikitext)
            if new_text != text:
                try:
                    self.site.save(page, new_text, summary=self.summary)
                except RetriedLoginAndStillFailed:
                    pass

    @staticmethod
    def get_team_tricode(team: LolGameTeam):
        for player in team.players:
            if " " in player.inGameName:
                return player.inGameName.split(" ")[0]
        return None
    
    def update_wikitext(self, wikitext, overview_page: str):
        for template in wikitext.filter_templates():
            if not template.name.matches('MatchSchedule/Game'):
                continue
            if not tl_has(template, 'riot_platform_game_id'):
                continue
            if tl_has(template, 'blue') and tl_has(template, 'red') and tl_has(template, 'winner'):
                continue
            platform_game_id = (
                template.get('riot_platform_game_id').value.strip()
            )
            try:
                summary, details = self.emh.get_game_data(platform_game_id)
                game_dto_summary = riot_transmute.v5.match_to_game(summary)
                game_dto_details = riot_transmute.v5.match_timeline_to_game(details)
                game = riot_transmute.merge_games_from_riot_match_and_timeline(
                    game_dto_summary, game_dto_details
                )
            except Exception as e:
                self.site.log_error_script(overview_page, e)
                continue
            blue = self.get_team_tricode(game.teams.BLUE)
            red = self.get_team_tricode(game.teams.RED)
            if not blue or not red:
                continue
            blue_team = self.site.cache.get_team_from_event_tricode(overview_page, blue)
            red_team = self.site.cache.get_team_from_event_tricode(overview_page, red)
            if not red_team or not blue_team:
                continue
            if blue_team is not None and red_team is not None:
                template.add('blue', blue_team)
                template.add('red', red_team)
                if game.winner == "BLUE":
                    template.add('winner', "1")
                elif game.winner == "RED":
                    template.add('winner', "2")
