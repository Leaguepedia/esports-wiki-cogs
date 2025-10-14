from mwrogue.esports_client import EsportsClient
from esports_cog_utils import utils
from redbot.core import commands, app_commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify


class SuperWLH(commands.Cog):
    """Queries the wiki to find database entries for a given player ID"""

    CARGO_TABLES = {
        "Players": "OverviewPage",
        "Contracts": "Player",
        "Entities": "Entity",
        "PlayerRedirects": "AllName",
        "ScoreboardPlayers": "Link",
        "TournamentPlayers": "Player",
        "Tenures": "Player",
        "RosterChanges": "Player"
    }

    def __init__(self, bot: Red, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot

    async def query(self, site, table, key_field, player):
        return site.cargo_client.query(
            tables=f"{table}=T",
            where=f"T.{key_field} = '{player}'",
            fields=f"T._pageName=Page",
            group_by="T._pageName"
        )

    async def run(self, player, ctx):
        site = await utils.login_if_possible(ctx, self.bot, 'lol')
        is_message_sent = False
        for table, key_field in self.CARGO_TABLES.items():
            response = await self.query(site, table, key_field, player)
            if response:
                message = []
                for item in response:
                    message.append(f"Found an entry in table `{table}`, stored from `{item['Page']}`")
                for page in pagify('\n'.join(message)):
                    await ctx.send(page)
                    is_message_sent = True
        if not is_message_sent:
            await ctx.send("No entries were found!")

    @commands.hybrid_command(name="superwlh")
    @app_commands.describe(player="The ID of the player you are looking entries for")
    async def superwlh(self, ctx: commands.Context, *, player: str):
        """Queries the wiki to find database entries for a given player ID"""
        await self.run(player, ctx)
