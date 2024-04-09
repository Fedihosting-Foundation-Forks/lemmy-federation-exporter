from datetime import datetime, timedelta, UTC
import logging
import os

import aiohttp.web
from prometheus_client.core import GaugeMetricFamily, Timestamp


from .prom_util import CollectorHelper


USER_AGENT = "Lemmy-Federation-Exporter (+https://github.com/Nothing4You/lemmy-federation-exporter)"
USER_AGENT = os.environ.get("HTTP_USER_AGENT", USER_AGENT)

logger = logging.getLogger(__name__)


async def metrics(request: aiohttp.web.Request) -> aiohttp.web.Response:
    instance = request.query.getone("instance")

    c = CollectorHelper()

    # last_retry = Last send try

    label_keys = (
        "remote_instance",
        "remote_software",
    )

    instance_last_seen_metric = GaugeMetricFamily(
        "lemmy_federation_state_instance_last_seen",
        "Timestamp instance was last seen",
        labels=label_keys,
    )
    c.add_metric(instance_last_seen_metric)
    instance_last_seen_metric_since_seconds = GaugeMetricFamily(
        "lemmy_federation_state_instance_last_seen_since_seconds",
        "Seconds since instance was last seen",
        labels=label_keys,
    )
    c.add_metric(instance_last_seen_metric_since_seconds)
    failure_count_metric = GaugeMetricFamily(
        "lemmy_federation_state_failure_count",
        "Failure count of last send",
        labels=label_keys,
    )
    c.add_metric(failure_count_metric)
    last_successful_id_metric = GaugeMetricFamily(
        "lemmy_federation_state_last_successful_id",
        "Last successfully sent activity id",
        labels=label_keys,
    )
    c.add_metric(last_successful_id_metric)
    last_successful_id_local_metric = GaugeMetricFamily(
        "lemmy_federation_state_last_successful_id_local",
        "Highest last successfully sent activity id across all linked instances",
    )
    c.add_metric(last_successful_id_local_metric)
    activities_behind_metric = GaugeMetricFamily(
        "lemmy_federation_state_activities_behind",
        "Distance between highest activity id and last successfully sent activity id",
        labels=label_keys,
    )
    c.add_metric(activities_behind_metric)
    last_successful_published_time_metric = GaugeMetricFamily(
        "lemmy_federation_state_last_successful_published_time",
        "Timestamp of last successfully sent activity",
        labels=label_keys,
    )
    c.add_metric(last_successful_published_time_metric)
    last_successful_published_since_seconds_metric = GaugeMetricFamily(
        "lemmy_federation_state_last_successful_published_since_seconds",
        "Seconds since last successfully sent activity",
        labels=label_keys,
    )
    c.add_metric(last_successful_published_since_seconds_metric)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as cs:
        r = await cs.get(
            f"https://{instance}/api/v3/federated_instances",
            headers={"user-agent": USER_AGENT},
        )
        r.raise_for_status()
        j = await r.json()

    # Get top 100 verified instances that have 2 endorsements and 1 gaurantor
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as cs:
        r = await cs.get(
            "https://fediseer.com/api/v1/whitelist",
            headers={"user-agent": USER_AGENT},
            params={
                "endorsements": 2,
                "guarantors": 1,
                "software_csv": "lemmy",
                "limit": 100,
                "domains": "true",
            },
        )
        r.raise_for_status()
        verified_domains = (await r.json())["domains"]

    now = datetime.now(UTC)
    cutoff_unseen_instances = now - timedelta(days=2)
    unix_epoch = datetime(1970, 1, 1, tzinfo=UTC)
    unix_now = now - unix_epoch
    prom_ts = Timestamp(unix_now.total_seconds(), unix_now.microseconds * 1000)

    # for instances using an allowlist, the same dicts are present in allowlist
    # if allowlist is empty, any linked instance is considered
    if len(j["federated_instances"]["allowed"]) > 0:
        federation_type = "allowed"
    else:
        federation_type = "linked"

    max_id = max(
        i.get("federation_state", {}).get("last_successful_id", 0)
        for i in j["federated_instances"][federation_type]
    )

    for i in j["federated_instances"][federation_type]:
        # Filter domains that are not verified
        if i["domain"] not in verified_domains:
            continue
        if "updated" not in i:
            logger.debug("[%s] missing updated for %s", instance, i["domain"])
            continue
        last_seen = datetime.fromisoformat(i["updated"])
        if last_seen < cutoff_unseen_instances:
            continue

        if "federation_state" not in i:
            logger.debug("[%s] missing federation_state for %s", instance, i["domain"])
            continue

        labels = (
            i["domain"],
            i.get("software", ""),
        )
        instance_last_seen_metric.add_metric(
            labels, (last_seen - unix_epoch).total_seconds(), prom_ts
        )
        instance_last_seen_metric_since_seconds.add_metric(
            labels,
            (now - last_seen).total_seconds(),
            prom_ts,
        )
        failure_count_metric.add_metric(
            labels, i["federation_state"]["fail_count"], prom_ts
        )
        last_successful_id_metric.add_metric(
            labels, i["federation_state"]["last_successful_id"], prom_ts
        )
        activities_behind_metric.add_metric(
            labels, max_id - i["federation_state"]["last_successful_id"], prom_ts
        )
        if "last_successful_published_time" in i["federation_state"]:
            last_successful_published_time_metric.add_metric(
                labels,
                (
                    datetime.fromisoformat(
                        i["federation_state"]["last_successful_published_time"]
                    )
                    - unix_epoch
                ).total_seconds(),
                prom_ts,
            )
            last_successful_published_since_seconds_metric.add_metric(
                labels,
                (
                    now
                    - datetime.fromisoformat(
                        i["federation_state"]["last_successful_published_time"]
                    )
                ).total_seconds(),
                prom_ts,
            )

    last_successful_id_local_metric.add_metric((), max_id, prom_ts)

    metrics_result = c.generate().decode()
    return aiohttp.web.Response(text=metrics_result)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO").upper(),
        format="%(asctime)s - %(levelname)8s - %(name)s:%(funcName)s - %(message)s",
    )

    logging.Formatter.formatTime = (  # type: ignore[method-assign]
        lambda self, record, datefmt: datetime.fromtimestamp(record.created, UTC)  # type: ignore
        .astimezone()
        .isoformat()
    )

    app = aiohttp.web.Application()
    app.add_routes(
        [
            aiohttp.web.get("/metrics", metrics),
        ]
    )
    aiohttp.web.run_app(app)


if __name__ == "__main__":
    main()
