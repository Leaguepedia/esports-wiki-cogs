from esports_cog_utils.utils import login_if_possible
from redbot.core import commands, checks


class MovePage(commands.Cog):
    """Fixes double redirects based on the wiki's Special:DoubleRedirects report"""
    
    def __init__(self, bot):
        self.bot = bot
        self.summary = 'Moving page + associated subpages'

    @commands.command()
    @checks.mod_or_permissions(manage_guild=True)
    async def move(self, ctx, wiki, p1, p2):
        site = await login_if_possible(ctx, self.bot, wiki)
        if site is None:
            return
        p1 = p1[0].upper() + p1[1:]
        for page in site.client.allpages(prefix=p1 + '/'):
            dest = page.name.replace(p1 + '/', p2 + '/')
            await ctx.send("Moving page {} to {}".format(page.name, dest))
            page.move(dest)
        
        return await ctx.send("Okay, should be done!")
