from redbot.core.bot import Red
from tsutils.errors import NoAPIKeyException
from bayesgamh.errors import RateLimitException, BadRequestException, NotFoundException

import backoff

from typing import Iterable, Literal, Optional, Union, TypedDict
from datetime import datetime

from aiohttp import ClientSession, ClientResponse

import re

FileType = Literal['summary', 'details']


class Tournament(TypedDict):
    id: str
    name: str
    parent: dict
    children: dict


class Series(TypedDict):
    id: str
    startTimeScheduled: str
    tournament: Tournament
    file_list: list


class GridFileData(TypedDict):
    id: str
    description: str
    status: str
    fileName: str
    fullURL: str


LOL_GRID_TITLE_ID = 3
LOL_GRID_DATA_PROVIDER = "LOL_LIVE"

END_STATE_FILE_ID_RE = r"^state-(summary|details)-riot-game-([0-9]+)$"

GRAPHQL_TOURNAMENT_FIELDS_STRING = """
id
name
parent {
    id
    name
}
children {
    id
    name
}
"""

GRAPHQL_SERIES_FIELDS_STRING = f"""
id
startTimeScheduled
tournament {{
    { GRAPHQL_TOURNAMENT_FIELDS_STRING }
}}
"""


class GridAPIWrapper:
    def __init__(self, bot: Red, session: ClientSession):
        self.bot = bot
        self.session = session

        self.tournament_cache = {}

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
    async def _cast_datetime(date: Union[str, datetime, None]) -> str:
        if isinstance(date, datetime):
            return date.isoformat()

        return date

    @staticmethod
    async def _split_if_needed(_str: Union[str, list, None]) -> Union[list, None]:
        if isinstance(_str, str):
            return _str.split(",")

        return _str

    async def _do_graphql_pagination(
            self,
            query_name: str,
            query: str,
            variables: dict,
            limit: Optional[int] = None
    ) -> list:
        response = await self._do_graphql_query(query=query, variables=variables)
        full_response = response
        ret = []
        while response[query_name]["pageInfo"]["hasNextPage"]:
            variables["after"] = response[query_name]["pageInfo"]["endCursor"]
            response = await self._do_graphql_query(query=query, variables=variables)
            full_response[query_name]["edges"].extend(response[query_name]["edges"])
            if limit and len(full_response[query_name]["edges"]) >= limit:
                break
        for row in full_response[query_name]["edges"]:
            ret.append(row["node"])
        return ret[:limit]

    async def get_series_list(
            self,
            limit: Optional[int] = None,
            gte: Optional[Union[str, datetime]] = None,
            lte: Optional[Union[str, datetime]] = None,
            tournament_ids: Optional[Union[Iterable[Union[str, int]], Union[str, int]]] = None,
            tournament_name: Optional[str] = None,
            grid_game_ids: Optional[Union[Iterable[str], str]] = None,
            return_parent_tournaments: Optional[bool] = False,
            return_file_list: Optional[bool] = False,
            invert_order: Optional[bool] = True,
            include_tournament_children: Optional[bool] = None,
    ) -> list[Series]:
        query = f"""
        query GetSeriesList($first: Int, $after: Cursor, $gte: String, $lte: String, $titleIds: [ID!], 
        $tournamentIds: [ID!], $seriesTypes: [SeriesType!], $gameIds: [ID!], $orderDirection: OrderDirection!
        $tournamentName: String, $includeChildren: Boolean) {{
            allSeries (
                first: $first
                after: $after
                orderBy: StartTimeScheduled
                orderDirection: $orderDirection
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
                        name: {{
                            equals: $tournamentName
                        }}
                        includeChildren: {{
                            equals: $includeChildren
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
            "titleIds": [LOL_GRID_TITLE_ID],
            # We only care about ESPORTS series
            "seriesTypes": ["ESPORTS"],
            "tournamentIds": await self._split_if_needed(tournament_ids),
            "tournamentName": tournament_name,
            "gameIds": await self._split_if_needed(grid_game_ids),
            "orderDirection": "DESC" if invert_order else "ASC",
            "includeChildren": include_tournament_children
        }

        series_list = await self._do_graphql_pagination("allSeries", query, variables, limit)

        ret = []

        for series in series_list:
            if return_parent_tournaments:
                series["tournament"] = await self.get_parent_tournament(series["tournament"]["id"])
            if return_file_list:
                series["file_list"] = await self.get_series_file_list(series["id"])
            ret.append(series)

        return ret

    async def get_parent_tournament(self, tournament_id: Union[str, int]) -> Tournament:
        if tournament_id in self.tournament_cache:
            return self.tournament_cache[tournament_id]

        children = []
        while True:
            children.append(tournament_id)
            response = await self.get_tournament(tournament_id)
            if response["parent"] is None:
                break
            tournament_id = response["parent"]["id"]

        for child in children:
            self.tournament_cache[child] = response

        return self.tournament_cache[tournament_id]

    async def get_series(self, series_id: Union[str, int]) -> Series:
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

        response = (await self._do_graphql_query(query=query, variables=variables))["series"]

        if response is None:
            raise BadRequestException(f"Series ID {series_id} was not found!")

        return response

    async def get_tournament(self, tournament_id: Union[str, int]) -> Tournament:
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

        response = (await self._do_graphql_query(query, variables))["tournament"]

        if response is None:
            raise BadRequestException(f"Tournament ID {tournament_id} was not found!")

        return response

    async def get_tournaments_list(
            self,
            has_parent: Optional[bool] = None,
            has_children: Optional[bool] = None,
            limit: Optional[int] = None,
            tournament_name: Optional[str] = None
    ) -> list[Tournament]:
        query = f"""
        query GetTournamentsList($after: Cursor, $titleId: ID!, $hasParent: Boolean, $hasChildren: Boolean, 
        $first: Int, $tournamentName: String) {{
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
                    name: {{
                        equals: $tournamentName
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
            "hasParent": has_parent,
            "hasChildren": has_children,
            "titleId": LOL_GRID_TITLE_ID,
            "tournamentName": tournament_name
        }

        return await self._do_graphql_pagination("tournaments", query, variables, limit)

    async def _do_graphql_game_id_by_external_id_query(self, platform_game_id: str) -> str:
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

        response = await self._do_api_call("POST", service, data=data)

        return response["data"]

    async def get_one_file_by_platform_game_id(self, platform_game_id: str, file_type: FileType) -> dict:
        if file_type not in ['summary', 'details']:
            raise BadRequestException("The file type must be summary or details.")
        summary, details = await self.get_files_by_platform_game_id(platform_game_id)
        if file_type == "summary":
            return summary
        else:
            if not details:
                raise NotFoundException
            return details

    async def get_files_by_platform_game_id(
            self,
            platform_game_id: str
    ) -> tuple[dict, Union[dict, None]]:
        platform_id, game_id = platform_game_id.split("_")

        series_id = (await self.get_series_data_by_platform_game_id(platform_game_id))["id"]
        file_list = await self.get_series_file_list(series_id)

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
            summary = await self.get_file("summary", series_id, game_sequence)
            if summary["platformId"] != platform_id or str(summary["gameId"]) != game_id:
                continue
            game_found = True
            try:
                details = await self.get_file("details", series_id, game_sequence)
            except NotFoundException:
                details = None
            break

        if not game_found:
            raise NotFoundException

        return summary, details

    async def get_series_data_by_platform_game_id(self, platform_game_id: str) -> dict:
        grid_game_id = await self._do_graphql_game_id_by_external_id_query(platform_game_id)
        if grid_game_id is None:
            raise NotFoundException
        series_data = (await self.get_series_list(grid_game_ids=grid_game_id))
        if not series_data:
            raise NotFoundException
        return series_data[0]

    async def get_series_file_list(
            self,
            series_id: str,
            only_end_state_files: Optional[bool] = True,
            filter_non_ready_files: Optional[bool] = True
    ) -> list[GridFileData]:
        response = (await self._do_api_call("GET", f"file-download/list/{series_id}"))["files"]

        ret = []

        for file in response:
            if (
                    (only_end_state_files and not re.match(END_STATE_FILE_ID_RE, file["id"])) or
                    (filter_non_ready_files and file["status"] != "ready")
            ):
                continue
            ret.append(file)

        return ret

    async def get_file(self, file_type: FileType, series_id: str, game_sequence: str) -> dict:
        return await self._do_api_call(
            "GET",
            f"file-download/end-state/riot/series/{series_id}/games/{game_sequence}/{file_type}"
        )

    @staticmethod
    async def _handle_response(response: ClientResponse) -> None:
        if response.status == 429:
            raise RateLimitException
        elif response.status == 403:
            # For some reason 403 means not found in the file-download API
            raise NotFoundException
        elif "application/json" in response.headers.get("content-type", ""):
            response_j = await response.json()
            # GRID doesn't like 429 errors
            if (
                    response_j.get("errors") and
                    response_j["errors"][0].get("extensions") and
                    response_j["errors"][0]["extensions"].get("errorDetail") == "ENHANCE_YOUR_CALM"
            ):
                raise RateLimitException
        response.raise_for_status()

    @backoff.on_exception(backoff.expo, RateLimitException, logger=None)
    async def _do_api_call(self, method: Literal['GET', 'POST'], route: str, data: Optional[dict] = None) -> dict:
        endpoint = "https://api.grid.gg/"

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
