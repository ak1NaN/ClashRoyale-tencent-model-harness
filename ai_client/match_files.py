"""Human-readable links to each match's canonical JSONL capture."""

import os
from pathlib import Path
import re


def _tag(value, fallback):
    cleaned = re.sub(r'[^\w-]+', '-', str(value).strip(), flags=re.UNICODE).strip('-_')
    return cleaned[:32] or fallback


def labeled_events_path(directory, *, started_at, label, checkpoint):
    directory = Path(directory)
    stamp = started_at.strftime('%Y-%m-%d_%H-%M-%S')
    match_id = directory.name.rsplit('-', 1)[-1]
    return directory / f'{stamp}_{_tag(label, "1v1")}_{_tag(Path(checkpoint).stem, "model")}_{match_id}.jsonl'


def link_labeled_events(directory, *, started_at, label, checkpoint):
    """Create a readable filename without duplicating a potentially huge log."""
    directory = Path(directory)
    source = directory / 'events.jsonl'
    destination = labeled_events_path(directory, started_at=started_at, label=label, checkpoint=checkpoint)
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        return destination
    try:
        os.link(source, destination)
    except OSError:
        destination.symlink_to(source.name)
    return destination
