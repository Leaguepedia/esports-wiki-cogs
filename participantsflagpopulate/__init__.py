from .participantsflagpopulate import ParticipantsFlagPopulate


async def setup(bot):
    n = ParticipantsFlagPopulate(bot)
    bot.add_cog(n) if not __import__('asyncio').iscoroutinefunction(bot.add_cog) else await bot.add_cog(n)
