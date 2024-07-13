from datetime import datetime, timezone
from typing import TYPE_CHECKING

from discord.ext.commands import BadArgument, Converter


class DateConverterClass(Converter):
    async def convert(self, ctx, argument) -> datetime:
        try:
            return datetime.fromisoformat(argument).astimezone(timezone.utc)
        except ValueError:
            raise BadArgument(f'Date must be in yyyy-mm-dd format, not `{argument}`.')


if not TYPE_CHECKING:
    DateConverter = DateConverterClass()
else:
    DateConverter = datetime
