from mwrogue.esports_client import EsportsClient
from esports_cog_utils import utils
from redbot.core import commands
from redbot.core.bot import Red

from .autorosters_main import AutoRostersRunner


async def is_lol_staff(ctx) -> bool:
    staff_role = None
    if not ctx.guild:
        raise commands.UserFeedbackCheckFailure("You must be in a server to run this command!")
    for role in ctx.message.guild.roles:
        if role.name == "LoL-Staff":
            staff_role = role
            break
    if staff_role not in ctx.author.roles:
        raise commands.UserFeedbackCheckFailure("You don't have enough permissions to run this command!")
    return True


class AutoRosters(commands.Cog):
    """Automatically generates team rosters for Leaguepedia, using scoreboard data"""
    
    def __init__(self, bot: Red, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot

    async def run(self, ctx, overview_page: str, query_coaches: bool = False):
        await ctx.send('Okay, starting now!')
        credentials = await utils.get_credentials(ctx, self.bot)
        site = EsportsClient('lol', credentials=credentials,
                             max_retries_mwc=0,
                             max_retries=2, retry_interval=10)
        overview_page = site.cache.get_target(overview_page)
        if not site.client.pages[overview_page].exists:
            return await ctx.send('The tournament page does not exist!')
        runner = AutoRostersRunner(site, overview_page, query_coaches)
        runner.run()
        username = site.credentials.username
        username = username.split('@')[0] if "@" in username else username
        sandbox_page = f"\nhttps://lol.fandom.com/wiki/User:{username}/Team_Rosters_Sandbox".replace(" ", "_")
        rosters_page = f"\nhttps://lol.fandom.com/wiki/{overview_page}/Team_Rosters".replace(" ", "_")
        done_message = "Okay, done!"
        if query_coaches:
            done_message += " **Remember to complete the coaches' fields!**"
        else:
            done_message += " **Remember the generated content has no coaches!**"
        await ctx.send(done_message)
        await ctx.send(f'Here is the sandbox page with the new content: <{sandbox_page}>')
        await ctx.send(f'Here is where you should copy it: <{rosters_page}>')
        await runner.send_warnings(ctx)
    
    @commands.command(pass_context=True)
    @commands.check(is_lol_staff)
    async def autorosters(self, ctx, *, overview_page):
        """Generate team rosters for the specified tournament"""
        await self.run(ctx, overview_page)

    @commands.command(pass_context=True)
    @commands.check(is_lol_staff)
    async def autorostersc(self, ctx, *, overview_page):
        """Generate team rosters for the specified tournament querying for team coaches"""
        await self.run(ctx, overview_page, query_coaches=True)
