import collections
import json
import re
from typing import Callable, Iterator, Optional, Sequence, Union

from openeo import Connection, DataCube
from openeo.rest.vectorcube import VectorCube

OPENEO_BACKEND = "https://openeo.test/"


class OpeneoTestingException(Exception):
    pass


class DummyBackend:
    """
    Dummy backend that handles sync/batch execution requests
    and allows inspection of posted process graphs
    """

    __slots__ = (
        "connection",
        "sync_requests",
        "batch_jobs",
        "validation_requests",
        "next_result",
        "next_validation_errors",
        "job_status_updater",
        "extra_job_metadata_fields",
    )

    # Default result (can serve both as JSON or binary data)
    DEFAULT_RESULT = b'{"what?": "Result data"}'

    def __init__(self, requests_mock, connection: Connection):
        self.connection = connection
        self.sync_requests = []
        self.batch_jobs = {}
        self.validation_requests = []
        self.next_result = self.DEFAULT_RESULT
        self.next_validation_errors = []
        self.extra_job_metadata_fields = []

        # Job status update hook:
        #   callable that is called on starting a job, and getting job metadata
        #   allows to dynamically change how the status of a job evolves
        #   By default: immediately set to "finished" once job is started
        self.job_status_updater = lambda job_id, current_status: "finished"

        requests_mock.post(
            connection.build_url("/result"),
            content=self._handle_post_result,
        )
        requests_mock.post(
            connection.build_url("/jobs"),
            content=self._handle_post_jobs,
        )
        requests_mock.post(
            re.compile(connection.build_url(r"/jobs/(job-\d+)/results$")), content=self._handle_post_job_results
        )
        requests_mock.get(re.compile(connection.build_url(r"/jobs/(job-\d+)$")), json=self._handle_get_job)
        requests_mock.get(
            re.compile(connection.build_url(r"/jobs/(job-\d+)/results$")), json=self._handle_get_job_results
        )
        requests_mock.get(
            re.compile(connection.build_url("/jobs/(.*?)/results/result.data$")),
            content=self._handle_get_job_result_asset,
        )
        requests_mock.post(connection.build_url("/validation"), json=self._handle_post_validation)

    def _handle_post_result(self, request, context):
        """handler of `POST /result` (synchronous execute)"""
        pg = request.json()["process"]["process_graph"]
        self.sync_requests.append(pg)
        result = self.next_result
        if isinstance(result, (dict, list)):
            result = json.dumps(result).encode("utf-8")
        elif isinstance(result, str):
            result = result.encode("utf-8")
        assert isinstance(result, bytes)
        return result

    def _handle_post_jobs(self, request, context):
        """handler of `POST /jobs` (create batch job)"""
        post_data = request.json()
        pg = post_data["process"]["process_graph"]
        job_id = f"job-{len(self.batch_jobs):03d}"
        job_data = {"job_id": job_id, "pg": pg, "status": "created"}
        for field in ["title", "description"]:
            if field in post_data:
                job_data[field] = post_data[field]
        for field in self.extra_job_metadata_fields:
            job_data[field] = post_data.get(field)
        self.batch_jobs[job_id] = job_data
        context.status_code = 201
        context.headers["openeo-identifier"] = job_id

    def _get_job_id(self, request) -> str:
        match = re.match(r"^/jobs/(job-\d+)(/|$)", request.path)
        if not match:
            raise OpeneoTestingException(f"Failed to extract job_id from {request.path}")
        job_id = match.group(1)
        assert job_id in self.batch_jobs
        return job_id

    def _handle_post_job_results(self, request, context):
        """Handler of `POST /job/{job_id}/results` (start batch job)."""
        job_id = self._get_job_id(request)
        assert self.batch_jobs[job_id]["status"] == "created"
        self.batch_jobs[job_id]["status"] = self.job_status_updater(
            job_id=job_id, current_status=self.batch_jobs[job_id]["status"]
        )
        context.status_code = 202

    def _handle_get_job(self, request, context):
        """Handler of `GET /job/{job_id}` (get batch job status and metadata)."""
        job_id = self._get_job_id(request)
        # Allow updating status with `job_status_setter` once job got past status "created"
        if self.batch_jobs[job_id]["status"] != "created":
            self.batch_jobs[job_id]["status"] = self.job_status_updater(
                job_id=job_id, current_status=self.batch_jobs[job_id]["status"]
            )
        return {"id": job_id, "status": self.batch_jobs[job_id]["status"]}

    def _handle_get_job_results(self, request, context):
        """Handler of `GET /job/{job_id}/results` (list batch job results)."""
        job_id = self._get_job_id(request)
        assert self.batch_jobs[job_id]["status"] == "finished"
        return {
            "id": job_id,
            "assets": {"result.data": {"href": self.connection.build_url(f"/jobs/{job_id}/results/result.data")}},
        }

    def _handle_get_job_result_asset(self, request, context):
        """Handler of `GET /job/{job_id}/results/result.data` (get batch job result asset)."""
        job_id = self._get_job_id(request)
        assert self.batch_jobs[job_id]["status"] == "finished"
        return self.next_result

    def _handle_post_validation(self, request, context):
        """Handler of `POST /validation` (validate process graph)."""
        pg = request.json()["process_graph"]
        self.validation_requests.append(pg)
        return {"errors": self.next_validation_errors}

    def get_sync_pg(self) -> dict:
        """Get one and only synchronous process graph"""
        assert len(self.sync_requests) == 1
        return self.sync_requests[0]

    def get_batch_pg(self) -> dict:
        """Get one and only batch process graph"""
        assert len(self.batch_jobs) == 1
        return self.batch_jobs[max(self.batch_jobs.keys())]["pg"]

    def get_pg(self, process_id: Optional[str] = None) -> dict:
        """
        Get one and only batch process graph (sync or batch)

        :param process_id: just return single process graph node with this process_id
        :return: process graph (flat graph representation) or process graph node
        """
        pgs = self.sync_requests + [b["pg"] for b in self.batch_jobs.values()]
        if len(pgs) != 1:
            raise OpeneoTestingException(f"Expected single process graph, but collected {len(pgs)}")
        pg = pgs[0]
        if process_id:
            # Just return single node (by process_id)
            found = [node for node in pg.values() if node.get("process_id") == process_id]
            if len(found) != 1:
                raise OpeneoTestingException(
                    f"Expected single process graph node with process_id {process_id!r}, but found {len(found)}: {found}"
                )
            return found[0]
        return pg

    def execute(self, cube: Union[DataCube, VectorCube], process_id: Optional[str] = None) -> dict:
        """
        Execute given cube (synchronously) and return observed process graph (or subset thereof).

        :param cube: cube to execute on dummy back-end
        :param process_id: just return single process graph node with this process_id
        :return: process graph (flat graph representation) or process graph node
        """
        cube.execute()
        return self.get_pg(process_id=process_id)

    def setup_simple_job_status_flow(self, *, queued: int = 1, running: int = 4, final: str = "finished"):
        """
        Set up simple job status flow:
        queued (a couple of times) -> running (a couple of times) -> finished/error.
        """
        template = ["queued"] * queued + ["running"] * running + [final]
        job_stacks = collections.defaultdict(template.copy)

        def get_status(job_id: str, current_status: str) -> str:
            stack = job_stacks[job_id]
            # Pop first item each time, but repeat the last one at the end
            return stack.pop(0) if len(stack) > 1 else stack[0]

        self.job_status_updater = get_status


def build_capabilities(
    *,
    api_version: str = "1.0.0",
    stac_version: str = "0.9.0",
    basic_auth: bool = True,
    oidc_auth: bool = True,
    collections: bool = True,
    processes: bool = True,
    sync_processing: bool = True,
    validation: bool = False,
    batch_jobs: bool = True,
    udp: bool = False,
) -> dict:
    """Build a dummy capabilities document for testing purposes."""

    endpoints = []
    if basic_auth:
        endpoints.append({"path": "/credentials/basic", "methods": ["GET"]})
    if oidc_auth:
        endpoints.append({"path": "/credentials/oidc", "methods": ["GET"]})
    if basic_auth or oidc_auth:
        endpoints.append({"path": "/me", "methods": ["GET"]})

    if collections:
        endpoints.append({"path": "/collections", "methods": ["GET"]})
        endpoints.append({"path": "/collections/{collection_id}", "methods": ["GET"]})
    if processes:
        endpoints.append({"path": "/processes", "methods": ["GET"]})
    if sync_processing:
        endpoints.append({"path": "/result", "methods": ["POST"]})
    if validation:
        endpoints.append({"path": "/validation", "methods": ["POST"]})
    if batch_jobs:
        endpoints.extend(
            [
                {"path": "/jobs", "methods": ["GET", "POST"]},
                {"path": "/jobs/{job_id}", "methods": ["GET", "DELETE"]},
                {"path": "/jobs/{job_id}/results", "methods": ["GET", "POST", "DELETE"]},
                {"path": "/jobs/{job_id}/logs", "methods": ["GET"]},
            ]
        )
    if udp:
        endpoints.extend(
            [
                {"path": "/process_graphs", "methods": ["GET"]},
                {"path": "/process_graphs/{process_graph_id", "methods": ["GET", "PUT", "DELETE"]},
            ]
        )

    capabilities = {
        "api_version": api_version,
        "stac_version": stac_version,
        "id": "dummy",
        "title": "Dummy openEO back-end",
        "description": "Dummy openeEO back-end",
        "endpoints": endpoints,
        "links": [],
    }
    return capabilities
