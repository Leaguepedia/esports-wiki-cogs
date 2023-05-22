from esports_cog_utils.utils import get_credentials
from mwrogue.esports_client import EsportsClient
from requests import ReadTimeout
from redbot.core import commands, app_commands
from tsutils.user_interaction import StatusManager

from mhtowinners.sbtowinners_main import SbToWinnersRunner
from mhtowinners.mhtowinners_main import MhToWinnersRunner


class MhToWinners(commands.Cog):
    """Commands to update MatchSchedule based on data from Scoreboards and Match History"""

    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot

    @commands.hybrid_command(name="sbtowinners", pass_context=True)
    @app_commands.describe(title_list="A comma separated list of tournament overview pages to update")
    async def sbtowinners(self, ctx, *, title_list: str = ""):
        """Updates MatchSchedule using Scoreboard data"""
        title_list = [title.strip() for title in title_list.split(",")]
        await self._do_the_thing(ctx, SbToWinnersRunner, title_list)

    @commands.hybrid_command(name="mhtowinners", pass_context=True)
    @app_commands.describe(title_list="A comma separated list of tournament overview pages to update")
    async def mhtowinners(self, ctx, *, title_list: str):
        """Updates MatchSchedule using match history data"""
        title_list = [title.strip() for title in title_list.split(",")]
        await self._do_the_thing(ctx, MhToWinnersRunner, title_list)

    async def _do_the_thing(self, ctx, the_thing, *args):
        await ctx.send('Okay, starting now!')
        credentials = await get_credentials(ctx, self.bot)
        site = EsportsClient('lol', credentials=credentials,
                             max_retries_mwc=0,
                             max_retries=2, retry_interval=10)
        try:
            async with StatusManager(self.bot):
                the_thing(site, *args).run()
        except ReadTimeout:
            return await ctx.send('Whoops, the site is taking too long to respond, try again later')
        await ctx.send('Okay, done!')
