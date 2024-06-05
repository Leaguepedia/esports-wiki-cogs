from redbot.core.bot import Red
from tsutils.errors import NoAPIKeyException
from bayesgamh.errors import RateLimitException, BadRequestException, NotFoundError

from typing import Any, Dict, Iterable, List, Literal, Optional, TypedDict, Union
from datetime import datetime

from aiohttp import ClientSession, ClientResponse

AssetType = Literal['summary', 'details']

LOL_GRID_TITLE_ID = 3
LOL_GRID_DATA_PROVIDER = "LOL_LIVE"

GRAPHQL_SERIES_FIELDS_STRING = """
id
startTimeScheduled
teams {
    baseInfo {
        name
    }
}
tournament {
    name
    id
    parent {
        id
    }
    children {
        id
    }
}
"""

GRAPHQL_TOURNAMENT_FIELDS_STRING = """
id
name
nameShortened
parent {
    id
}
children {
    id
}
"""


class GridAPIWrapper:
    def __init__(self, bot: Red, session: ClientSession):
        self.bot = bot
        self.session = session

        self.api_token = None

    async def _get_api_token(self) -> None:
        grid_tokens = await self.bot.get_shared_api_tokens("grid")
        if "x-api-key" not in grid_tokens:
            raise NoAPIKeyException((await self.bot.get_valid_prefixes())[0] + f"set api grid x-api-key <API_KEY>")
        self.api_token = grid_tokens["x-api-key"]

    async def _get_headers(self) -> dict:
        if not self.api_token:
            await self._get_api_token()
        return {
            "x-api-key": self.api_token,
            "Accept": "application/json",
        }

    @staticmethod
    async def _cast_datetime(date: Union[str, datetime, None]):
        if isinstance(date, datetime):
            return date.isoformat()

        return date

    @staticmethod
    async def _join_list_if_needed(_list: Union[list, None]) -> Union[str, None]:
        if isinstance(_list, list):
            _list = [str(item) for item in _list]
            return ",".join(_list)

        return _list

    async def _do_graphql_pagination(
            self,
            query_name: str,
            query: str,
            variables: dict,
            limit: Optional[int] = None
    ) -> list:
        response = await self._do_graphql_query(query=query, variables=variables)
        ret = response
        while response[query_name]["pageInfo"]["hasNextPage"] or len(ret[query_name]["edges"]) >= limit:
            variables["after"] = response[query_name]["pageInfo"]["endCursor"]
            response = await self._do_graphql_query(query=query, variables=variables)
            ret[query_name]["edges"].extend(response[query_name]["edges"])
        return response[query_name]["edges"][:limit]

    async def _do_graphql_allseries_query(
            self,
            limit: Optional[int] = None,
            gte: Optional[Union[str, datetime]] = None,
            lte: Optional[Union[str, datetime]] = None,
            tournament_ids: Optional[Union[Iterable[Union[str, int]], Union[str, int]]] = None,
            series_types: Optional[Union[Iterable[str], str]] = None,
            grid_game_ids: Optional[Union[Iterable[str], str]] = None
    ) -> list:
        query = f"""
        query GetSeriesList($first: Int, $after: Cursor, $gte: String, $lte: String, $titleIds: [ID!], 
        $tournamentIds: [ID!], $seriesTypes: [SeriesType!], $gameIds: [ID!]) {{
            allSeries (
                first: $first
                after: $after
                filter: {{
                    startTimeScheduled: {{
                        gte: $gte
                        lte: $lte
                    }}
                    titleIds: {{
                        in: $titleIds
                    }}
                    tournament: {{
                        id: {{
                            in: $tournamentIds  
                        }}
                    }}
                    types: $seriesTypes
                    live: {{
                        games: {{
                            id: {{
                                in: $gameIds
                            }}
                        }}
                    }}
                }}
            ) {{
                totalCount
                pageInfo {{
                    hasPreviousPage
                    hasNextPage
                    startCursor
                    endCursor
                }}
                edges {{
                    node {{
                        {GRAPHQL_SERIES_FIELDS_STRING}
                    }}
                }}
            }}
        }}
        """

        variables = {
            "first": 50,
            "gte": await self._cast_datetime(gte),
            "lte": await self._cast_datetime(lte),
            "titleIds": LOL_GRID_TITLE_ID,
            "tournamentIds": await self._join_list_if_needed(tournament_ids),
            "seriesTypes": await self._join_list_if_needed(series_types),
            "gameIds": await self._join_list_if_needed(grid_game_ids)
        }

        return await self._do_graphql_pagination("allSeries", query, variables, limit)

    async def _do_graphql_series_query(self, series_id: Union[str, int]) -> dict:
        query = f"""
        query GetSeries($seriesId: ID!) {{
            series (
                id: $seriesId
            )
            {{
                {GRAPHQL_SERIES_FIELDS_STRING}
            }}
        }}
        """

        variables = {
            "seriesId": str(series_id)
        }

        return (await self._do_graphql_query(query=query, variables=variables))["series"]

    async def _do_graphql_tournament_query(self, tournament_id: Union[str, int]) -> dict:
        query = f"""
        query GetTournament($tournamentId: ID!) {{
            tournament (
                id: $tournamentId
            )
            {{
                {GRAPHQL_TOURNAMENT_FIELDS_STRING}
            }}
        }}
        """

        variables = {
            "tournamentId": str(tournament_id)
        }

        return (await self._do_graphql_query(query, variables))["tournament"]

    async def _do_graphql_tournaments_query(
            self,
            has_parent: Optional[bool] = None,
            has_children: Optional[bool] = None,
            limit: Optional[int] = None
    ) -> list:
        query = f"""
        query GetTournamentsList($after: Cursor, $titleId: ID!, $hasParent: Boolean, $hasChildren: Boolean, 
        $first: Int) {{
            tournaments (
                after: $after
                first: $first
                filter: {{
                    titleId: $titleId
                    hasParent: {{
                        equals: $hasParent
                    }}
                    hasChildren: {{
                        equals: $hasChildren
                    }}
                }}
            )
            {{
                totalCount
                pageInfo {{
                    hasPreviousPage
                    hasNextPage
                    startCursor
                    endCursor
                }}
                edges {{
                    node {{
                        {GRAPHQL_TOURNAMENT_FIELDS_STRING}
                    }}
                }}
            }}
        }}
        """

        variables = {
            "first": 50,
            "hasParent": str(has_parent).lower(),
            "hasChildren": str(has_children).lower(),
            "titleId": LOL_GRID_TITLE_ID,
        }

        return await self._do_graphql_pagination("tournaments", query, variables, limit)

    async def get_series_data_by_platform_game_id(self, platform_game_id: str) -> dict:
        grid_game_id = await self._do_graphql_game_id_by_external_id_query(platform_game_id)
        if grid_game_id is None:
            raise NotFoundError
        series_data = (await self._do_graphql_allseries_query(grid_game_ids=grid_game_id))
        if not series_data:
            raise NotFoundError
        return series_data[0]["node"]

    async def _do_graphql_game_id_by_external_id_query(self, platform_game_id: str) -> dict:
        query = """
        query GetGameIdByExternalId($dataProviderName: String!, $externalGameId: ID!) {
            gameIdByExternalId (
                dataProviderName: $dataProviderName
                externalGameId: $externalGameId
            )
        }
        """

        variables = {
            "dataProviderName": LOL_GRID_DATA_PROVIDER,
            "externalGameId": platform_game_id,
        }

        return (await self._do_graphql_query(query=query, variables=variables))["gameIdByExternalId"]

    async def _do_graphql_query(self, query: str, variables: dict = None) -> dict:
        service = "central-data/graphql"
        data = {
            "query": query,
            "variables": variables,
        }
        return (await self._do_api_call("POST", service, data=data))["data"]

    # This function sucks
    async def get_assets(
            self,
            platform_game_id: str
    ) -> Union[tuple[dict, dict], tuple[None, None]]:
        platform_id, game_id = platform_game_id.split("_")

        series_id = (await self.get_series_data_by_platform_game_id(platform_game_id))["id"]
        file_list = await self._do_filedownload_list_games_query(series_id)

        # Now we need to go through every riot end state summary file with ready as its status,
        # download the file and check if platformId and gameId matches the given platform_game_id,
        # because it could be any other game from the same series
        # Once we found the game, we return the summary and corresponding details for the same game sequence

        game_found = False
        summary, details = None, None

        for file_data in file_list:
            if file_data["status"] != "ready" or not file_data["id"].startswith("state-summary-riot"):
                continue
            game_sequence = file_data["id"].split("-")[4]
            summary = await self._do_filedownload_download_query("summary", series_id, game_sequence)
            if summary["platformId"] != platform_id or str(summary["gameId"]) != game_id:
                continue
            game_found = True
            try:
                details = await self._do_filedownload_download_query("details", series_id, game_sequence)
            except NotFoundError:
                details = None
            break

        if not game_found:
            return None, None

        return summary, details

    async def _do_filedownload_list_games_query(self, series_id: str) -> list:
        return (await self._do_api_call("GET", f"file-download/list/{series_id}"))["files"]

    async def _do_filedownload_download_query(self, asset: AssetType, series_id: str, game_sequence: str) -> dict:
        return await self._do_api_call(
            "GET",
            f"file-download/end-state/riot/series/{series_id}/games/{game_sequence}/{asset}"
        )

    @staticmethod
    async def _handle_response(response: ClientResponse) -> None:
        if response.status == 429:
            raise RateLimitException
        elif response.status == 403:
            # For some reason 403 means not found in the file-download API
            raise NotFoundError
        response.raise_for_status()

    async def _do_api_call(self, method: Literal['GET', 'POST'], route: str, data: Optional[dict] = None) -> dict:
        endpoint = "https://api.grid.gg/"

        print(endpoint + route)
        print(data)
        print(await self._get_headers())

        if method == "GET":
            async with self.session.get(endpoint + route, params=data, headers=await self._get_headers()) as resp:
                await self._handle_response(resp)
                # content_type None because some files are returned as text/plain instead of application/json
                response = await resp.json(content_type=None)
        elif method == "POST":
            async with self.session.post(endpoint + route, json=data, headers=await self._get_headers()) as resp:
                await self._handle_response(resp)
                response = await resp.json()
        else:
            raise ValueError("HTTP Method must be GET or POST.")

        return response
