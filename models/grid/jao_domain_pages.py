"""Paged retrieval and completeness validation for JAO UTC-hour or delivery-day domains."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def fetch_hour_uncached(
    hour_start_utc: datetime,
    *,
    timeout_seconds: float,
    request_json: Callable[..., Any],
    domain_url: str,
    iso_z: Callable[..., str],
    parse_publication_utc: Callable[[Any], datetime | None],
    parse_nonnegative_int: Callable[[Any], int | None],
    normalize_records: Callable[[dict[str, Any]], list[dict[str, Any]]],
    page_size: int,
    max_pages: int,
) -> dict[str, Any]:
    """Fetch one UTC hour via skip/take pagination and validate complete retrieval."""
    if hour_start_utc.tzinfo is None:
        raise ValueError("JAO hour timestamp must be timezone-aware.")
    hour_start_utc = hour_start_utc.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hour_end_utc = hour_start_utc + timedelta(hours=1)

    all_rows: list[dict[str, Any]] = []
    seen_row_ids: set[Any] = set()
    duplicate_row_count = 0
    page_last_modified_raw: list[str] = []
    page_last_modified_parsed: list[datetime] = []
    page_row_counts: list[int] = []
    all_messages: list[Any] = []
    metadata_consistent = True

    expected_row_count: int | None = None
    expected_count_source: str | None = None
    total_rows_meta: int | None = None
    total_rows_with_filter_meta: int | None = None

    skip = 0
    page_index = 0

    while page_index < max_pages:
        payload = request_json(
            domain_url,
            {
                "FromUtc": iso_z(hour_start_utc, milliseconds=True),
                "ToUtc": iso_z(hour_end_utc, milliseconds=True),
                "Skip": str(skip),
                "Take": str(page_size),
            },
            source_label=f"JAO Nordic finalComputation (hour {hour_start_utc.strftime('%H:00')}, page {page_index + 1})",
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(
                f"JAO finalComputation response on page {page_index + 1} has no list-valued 'data' field."
            )
        if payload.get("rejected") is True:
            raise RuntimeError(
                f"JAO finalComputation rejected page {page_index + 1}: {payload.get('messages') or 'no message'}"
            )

        page_rows = payload["data"]
        page_row_counts.append(len(page_rows))
        raw_last_modified = payload.get("lastModifiedOn")
        if raw_last_modified is not None:
            page_last_modified_raw.append(str(raw_last_modified))
            parsed_ts = parse_publication_utc(raw_last_modified)
            if parsed_ts is not None:
                page_last_modified_parsed.append(parsed_ts)

        if payload.get("messages"):
            all_messages.append(payload["messages"])

        if page_index == 0:
            total_rows_meta = parse_nonnegative_int(payload.get("totalRows"))
            total_rows_with_filter_meta = parse_nonnegative_int(payload.get("totalRowsWithFilter"))
            if total_rows_with_filter_meta is not None:
                expected_row_count = total_rows_with_filter_meta
                expected_count_source = "totalRowsWithFilter"
            elif total_rows_meta is not None:
                expected_row_count = total_rows_meta
                expected_count_source = "totalRows"
        else:
            current_filtered = parse_nonnegative_int(payload.get("totalRowsWithFilter"))
            current_total = parse_nonnegative_int(payload.get("totalRows"))
            current_expected = current_filtered if current_filtered is not None else current_total
            if current_expected != expected_row_count:
                metadata_consistent = False

        for row in page_rows:
            if isinstance(row, dict):
                # `mrId` can identify the same CNEC across many MTUs. Include
                # delivery time in the key so that a repeated constraint across
                # two different MTUs is not misclassified as a duplicate page row.
                row_identity = row.get("id") or row.get("mrId") or row.get("cnecName")
                row_key = (str(row.get("dateTimeUtc") or ""), str(row_identity))
                if row_identity is not None:
                    if row_key in seen_row_ids:
                        duplicate_row_count += 1
                    else:
                        seen_row_ids.add(row_key)
                all_rows.append(row)

        page_index += 1
        skip += len(page_rows)

        if not page_rows:
            break
        if expected_row_count is not None:
            if skip >= expected_row_count:
                break
        elif len(page_rows) < page_size:
            break

    returned_row_count = len(all_rows)
    publication_version_consistent = (
        len(page_last_modified_parsed) == page_index
        and len(set(page_last_modified_parsed)) <= 1
    )

    if expected_row_count is None:
        dataset_complete = False
        completeness_reason = "JAO_RESPONSE_ROW_COUNT_METADATA_MISSING"
    elif returned_row_count != expected_row_count:
        dataset_complete = False
        completeness_reason = (
            f"JAO_HOURLY_RESPONSE_ROW_COUNT_MISMATCH: expected {expected_row_count} "
            f"({expected_count_source}), retrieved {returned_row_count} across {page_index} pages"
        )
    elif duplicate_row_count > 0:
        unique_count = len(seen_row_ids)
        if unique_count != expected_row_count:
            dataset_complete = False
            completeness_reason = (
                f"JAO_HOURLY_RESPONSE_DUPLICATE_ROWS: {duplicate_row_count} duplicate rows detected, "
                f"unique {unique_count} != expected {expected_row_count}"
            )
        else:
            dataset_complete = True
            completeness_reason = None
    else:
        dataset_complete = True
        completeness_reason = None

    if not metadata_consistent:
        dataset_complete = False
        completeness_reason = "JAO_HOURLY_ROW_COUNT_METADATA_CHANGED_DURING_PAGINATION"
    if not publication_version_consistent:
        dataset_complete = False
        completeness_reason = "JAO_HOURLY_PUBLICATION_VERSION_CHANGED_DURING_PAGINATION"

    if publication_version_consistent and page_last_modified_parsed:
        publication_metadata_complete = True
        last_modified_on_utc = iso_z(page_last_modified_parsed[0], milliseconds=True)
    else:
        publication_metadata_complete = False
        last_modified_on_utc = None

    records = normalize_records({"data": all_rows})

    return {
        "records": records,
        "last_modified_on_utc": last_modified_on_utc,
        "publication_metadata_complete": publication_metadata_complete,
        "raw_last_modified_on": page_last_modified_raw[0] if page_last_modified_raw else None,
        "page_last_modified_raw": page_last_modified_raw,
        "page_row_counts": page_row_counts,
        "page_count": page_index,
        "metadata_consistent": metadata_consistent,
        # Kept for diagnostic compatibility. Page-envelope timestamps are not
        # required to match; publication stability is checked via monitoring.
        "publication_version_consistent": None,
        "page_timestamp_metadata_complete": page_timestamp_metadata_complete,
        "page_timestamp_spread_seconds": page_timestamp_spread_seconds,
        "duplicate_row_count": duplicate_row_count,
        "dataset_complete": dataset_complete,
        "dataset_completeness_reason": completeness_reason,
        "returned_row_count": returned_row_count,
        "expected_row_count": expected_row_count,
        "expected_row_count_source": expected_count_source,
        "total_rows": total_rows_meta,
        "total_rows_with_filter": total_rows_with_filter_meta,
        "messages": all_messages if all_messages else None,
    }




def fetch_day_uncached(
    day_start_utc: datetime,
    day_end_utc: datetime,
    *,
    timeout_seconds: float,
    request_json: Callable[..., Any],
    domain_url: str,
    iso_z: Callable[..., str],
    parse_publication_utc: Callable[[Any], datetime | None],
    parse_nonnegative_int: Callable[[Any], int | None],
    normalize_records: Callable[[dict[str, Any]], list[dict[str, Any]]],
    page_size: int,
    max_pages: int,
) -> dict[str, Any]:
    """Fetch one complete Oslo delivery day, then group normalized CNECs by MTU.

    JAO's endpoint accepts a FromUtc/ToUtc range. Requesting the delivery day
    avoids one serialized HTTP request per UTC hour while retaining bounded
    pagination, row-count validation, response-vintage checks and per-MTU
    completeness checks.
    """
    if day_start_utc.tzinfo is None or day_end_utc.tzinfo is None:
        raise ValueError("JAO day bounds must be timezone-aware.")
    start = day_start_utc.astimezone(timezone.utc)
    end = day_end_utc.astimezone(timezone.utc)
    if end <= start or (end - start).total_seconds() % 900 != 0:
        raise ValueError("JAO day bounds must be an increasing 15-minute-aligned interval.")

    all_rows: list[dict[str, Any]] = []
    seen_row_ids: set[tuple[str, str]] = set()
    duplicate_row_count = 0
    page_last_modified_raw: list[str] = []
    page_last_modified_parsed: list[datetime] = []
    page_row_counts: list[int] = []
    all_messages: list[Any] = []
    metadata_consistent = True
    expected_row_count: int | None = None
    expected_count_source: str | None = None
    total_rows_meta: int | None = None
    total_rows_with_filter_meta: int | None = None
    skip = 0
    page_index = 0
    current_page_size = max(1, page_size)
    min_page_size = min(current_page_size, 2500)
    max_total_pages = max(max_pages, 200)

    while page_index < max_total_pages:
        if current_page_size == page_size and page_index >= max_pages:
            break
        payload = request_json(
            domain_url,
            {"FromUtc": iso_z(start, milliseconds=True),
            "ToUtc": iso_z(end, milliseconds=True),
            "Skip": str(skip),
            "Take": str(current_page_size),
            },
            source_label=(
                f"JAO Nordic finalComputation (delivery day "
                f"{start.isoformat()} to {end.isoformat()}, page {page_index + 1}, take {current_page_size})"
            ),
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(
                f"JAO finalComputation day response on page {page_index + 1} "
                "has no list-valued 'data' field."
            )
        if payload.get("rejected") is True:
            raise RuntimeError(
                f"JAO finalComputation rejected day page {page_index + 1}: "
                f"{payload.get('messages') or 'no message'}"
            )

        page_rows = payload["data"]
        page_row_counts.append(len(page_rows))
        raw_last_modified = payload.get("lastModifiedOn")
        if raw_last_modified is not None:
            page_last_modified_raw.append(str(raw_last_modified))
            parsed_ts = parse_publication_utc(raw_last_modified)
            if parsed_ts is not None:
                page_last_modified_parsed.append(parsed_ts)
        if payload.get("messages"):
            all_messages.append(payload["messages"])

        if page_index == 0:
            total_rows_meta = parse_nonnegative_int(payload.get("totalRows"))
            total_rows_with_filter_meta = parse_nonnegative_int(payload.get("totalRowsWithFilter"))
            if total_rows_with_filter_meta is not None:
                expected_row_count = total_rows_with_filter_meta
                expected_count_source = "totalRowsWithFilter"
            elif total_rows_meta is not None:
                expected_row_count = total_rows_meta
                expected_count_source = "totalRows"
        else:
            current_filtered = parse_nonnegative_int(payload.get("totalRowsWithFilter"))
            current_total = parse_nonnegative_int(payload.get("totalRows"))
            current_expected = current_filtered if current_filtered is not None else current_total
            if current_expected != expected_row_count:
                metadata_consistent = False

        for row in page_rows:
            if not isinstance(row, dict):
                continue
            row_identity = row.get("id") or row.get("mrId") or row.get("cnecName")
            row_key = (str(row.get("dateTimeUtc") or ""), str(row_identity))
            if row_identity is not None:
                if row_key in seen_row_ids:
                    duplicate_row_count += 1
                else:
                    seen_row_ids.add(row_key)
            all_rows.append(row)

        page_index += 1
        skip += len(page_rows)
        if not page_rows:
            break
        if expected_row_count is not None:
            if skip >= expected_row_count:
                break
        elif len(page_rows) < current_page_size:
            break

    returned_row_count = len(all_rows)
    # finalComputation's root-level lastModifiedOn is response-envelope
    # metadata. Separate page requests may report different values even when
    # they read the same published CNE domain, so exact equality is not a valid
    # publication-version test. The stable publication identity is checked by
    # the caller against JAO's monitoring endpoint before and after pagination.
    page_timestamp_metadata_complete = len(page_last_modified_parsed) == page_index
    page_timestamp_spread_seconds = (
        (max(page_last_modified_parsed) - min(page_last_modified_parsed)).total_seconds()
        if page_timestamp_metadata_complete and page_last_modified_parsed
        else None
    )
    if expected_row_count is None:
        dataset_complete = False
        completeness_reason = "JAO_RESPONSE_ROW_COUNT_METADATA_MISSING"
    elif returned_row_count != expected_row_count:
        dataset_complete = False
        completeness_reason = (
            f"JAO_DELIVERY_DAY_ROW_COUNT_MISMATCH: expected {expected_row_count} "
            f"({expected_count_source}), retrieved {returned_row_count} across {page_index} pages"
        )
    elif duplicate_row_count:
        unique_count = len(seen_row_ids)
        dataset_complete = unique_count == expected_row_count
        completeness_reason = None if dataset_complete else (
            f"JAO_DELIVERY_DAY_DUPLICATE_ROWS: {duplicate_row_count} duplicate rows, "
            f"unique {unique_count} != expected {expected_row_count}"
        )
    else:
        dataset_complete = True
        completeness_reason = None

    if not metadata_consistent:
        dataset_complete = False
        completeness_reason = "JAO_DELIVERY_DAY_ROW_COUNT_METADATA_CHANGED_DURING_PAGINATION"
    # Missing page-envelope timestamps do not by themselves invalidate the
    # row set. The caller can use the stable monitoring publication timestamp
    # as the vintage source after its before/after check.

    # Use the latest page timestamp as a conservative cutoff. The independent
    # before/after monitoring-signature check in flow_based_domain.py guards
    # against an actual CNE publication change while pagination is in flight.
    if page_timestamp_metadata_complete and page_last_modified_parsed:
        last_modified_on_utc = iso_z(max(page_last_modified_parsed), milliseconds=True)
        publication_metadata_complete = True
    else:
        publication_metadata_complete = False
        last_modified_on_utc = None

    normalized_records = normalize_records({"data": all_rows})
    records_by_mtu: dict[str, list[dict[str, Any]]] = {}
    for record in normalized_records:
        key = record.get("delivery_start_utc")
        if key:
            records_by_mtu.setdefault(str(key), []).append(record)

    expected_mtu_count = int((end - start).total_seconds() // 900)
    expected_mtu_keys = [iso_z(start + timedelta(minutes=15 * index)) for index in range(expected_mtu_count)]
    missing_mtu_keys = [key for key in expected_mtu_keys if not records_by_mtu.get(key)]
    if dataset_complete and missing_mtu_keys:
        dataset_complete = False
        completeness_reason = (
            "JAO_DELIVERY_DAY_HAS_MTUS_WITHOUT_ACTIVE_CNECS: "
            + ", ".join(missing_mtu_keys[:8])
        )

    return {
        "records_by_mtu": records_by_mtu,
        "last_modified_on_utc": last_modified_on_utc,
        "publication_metadata_complete": publication_metadata_complete,
        "raw_last_modified_on": page_last_modified_raw[0] if page_last_modified_raw else None,
        "page_last_modified_raw": page_last_modified_raw,
        "page_row_counts": page_row_counts,
        "page_count": page_index,
        "metadata_consistent": metadata_consistent,
        # Kept for diagnostic compatibility. Page-envelope timestamps are not
        # required to match; publication stability is checked via monitoring.
        "publication_version_consistent": None,
        "page_timestamp_metadata_complete": page_timestamp_metadata_complete,
        "page_timestamp_spread_seconds": page_timestamp_spread_seconds,
        "duplicate_row_count": duplicate_row_count,
        "dataset_complete": dataset_complete,
        "dataset_completeness_reason": completeness_reason,
        "returned_row_count": returned_row_count,
        "expected_row_count": expected_row_count,
        "expected_row_count_source": expected_count_source,
        "total_rows": total_rows_meta,
        "total_rows_with_filter": total_rows_with_filter_meta,
        "normalized_record_count": len(normalized_records),
        "expected_mtu_count": expected_mtu_count,
        "mtus_with_records": len(records_by_mtu),
        "missing_mtu_keys": missing_mtu_keys,
        "messages": all_messages if all_messages else None,
    }
