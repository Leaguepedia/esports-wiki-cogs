from mwrogue.esports_client import EsportsClient
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify


class SuperWLH(commands.Cog):
    """Queries the wiki to check for duplicated player entries"""

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
        self.site = EsportsClient('lol')

    async def query(self, table, key_field, player):
        return self.site.cargo_client.query(
            tables=f"{table}=T",
            where=f"T.{key_field} = '{player}'",
            fields=f"T._pageName=Page",
            group_by="T._pageName"
        )

    @commands.command(pass_context=True)
    async def superwlh(self, ctx, *, player):
        for table, key_field in self.CARGO_TABLES.items():
            response = await self.query(table, key_field, player)
            if response:
                message = []
                for item in response:
                    message.append(f"Found an entry in table `{table}`, stored from `{item['Page']}`")
                for page in pagify('\n'.join(message)):
                    await ctx.send(page)
