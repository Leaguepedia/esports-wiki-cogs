from mwrogue.esports_client import EsportsClient
from esports_cog_utils import utils
from redbot.core import commands, app_commands
from redbot.core.bot import Red
import mwparserfromhell
from discord import DMChannel


async def is_guild(ctx) -> bool:
    if isinstance(ctx.channel, DMChannel):
        raise commands.UserFeedbackCheckFailure("This command is not available in DMs")
    return True


class ParticipantsFlagPopulate(commands.Cog):
    """Populates flags for every player with a page on a given tournament"""

    def __init__(self, bot: Red, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot

    async def run(self, ctx, overview_page):
        await ctx.send('Okay, starting now!')
        credentials = await utils.get_credentials(ctx, self.bot)
        site = EsportsClient('lol', credentials=credentials,
                             max_retries_mwc=0,
                             max_retries=2, retry_interval=10)

        page_title = site.cache.get_target(overview_page.strip())
        page = site.client.pages[page_title]
        if not page.exists:
            return await ctx.send("The tournament page does not exist!")
        page_text = page.text()
        page_wikitext = mwparserfromhell.parse(page_text)

        for template in page_wikitext.filter_templates():
            if not template.name.matches("TeamRoster/Line"):
                continue
            if not template.has("player"):
                continue
            if template.get("player").value.strip() == "":
                continue
            player_name = template.get("player").value.strip()
            player_data = site.cargo_client.query(
                tables="PlayerRedirects=PR, Players=P",
                join_on="PR.OverviewPage=P.OverviewPage",
                fields="COALESCE(P.NationalityPrimary, P.Country)=Flag",
                where=f"PR.AllName = '{player_name}'"
            )
            if not player_data:
                continue
            if len(player_data) > 1:
                continue
            player_data = player_data[0]
            if not player_data["Flag"]:
                continue
            template.add(name="flag", value=site.cache.get("Country", player_data["Flag"], "flag"))

        if page_text != str(page_wikitext):
            site.save_title(title=page_title, text=str(page_wikitext), summary="Automatically populating player flags")

        await ctx.send("Okay, done!")

    @commands.hybrid_command(name="participantsflagpopulate", pass_context=True)
    @app_commands.describe(overview_page="The overview page of the tournament")
    @commands.check(is_guild)
    async def participantsflagpopulate(self, ctx, *, overview_page: str):
        """Populates flags for every player with a page on a given tournament"""
        await self.run(ctx, overview_page)
