"""Snapshot explicitly linked model output images, not arbitrary files or URLs."""
import hashlib
import os
from pathlib import Path
import re
import stat

from .assets import _image

MAX_PHOTO = 10_000_000
MAX_STORED = 40_000_000
IMAGE = re.compile(r'!\[([^\]\n]{0,200})\]\(\s*(?:<([^>\n]+)>|([^\)\n]+))\s*\)')


def read_photo(path, config):
    path = Path(path)
    root = config.cwd
    if (not path.is_absolute() or '..' in path.parts or not path.is_relative_to(root)
        or path.suffix.lower() not in {'.png', '.jpg', '.jpeg'}):
        raise ValueError('unsafe_output_image')
    relative = path.relative_to(root)
    denied = (config.codex_home, config.data_dir, root / 'personal-agent')
    if (any(part.startswith('.') for part in relative.parts)
        or any(path.is_relative_to(p) for p in denied)):
        raise ValueError('protected_output_image')
    # Open each parent by directory descriptor: no symlink traversal or parent
    # replacement race. Never read from the original path again at delivery time.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or not 0 < before.st_size <= MAX_PHOTO):
                raise ValueError('unsafe_output_image')
            data = source.read(MAX_PHOTO + 1)
            after = os.fstat(source.fileno())
            if (len(data) != before.st_size or len(data) > MAX_PHOTO
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                   (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError('output_image_changed')
    finally:
        os.close(directory)
    mime, _, width, height = _image(data, 20_000_000)
    if width + height > 10000 or max(width, height) > min(width, height) * 20:
        raise ValueError('photo_dimensions_exceeded')
    return data, mime, hashlib.sha256(data).hexdigest()


def prepare_images(text, config):
    # Ignore code examples; only actual markdown image references are exported.
    text = re.sub(r'```.*?```|`[^`\n]*`', '', text, flags=re.S)
    prepared, seen, rejected = [], set(), 0
    for match in IMAGE.finditer(text):
        path = (match.group(2) or match.group(3)).strip()
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > 3:
            rejected += 1
            break
        try:
            data, mime, digest = read_photo(path, config)
        except (OSError, ValueError):
            rejected += 1
            continue
        prepared.append({'reference': match.group(0), 'caption': match.group(1)[:150],
                         'data': data, 'mime': mime, 'sha256': digest})
    return prepared, rejected


def queue_images(store, config, job, prepared, key, parent_id=None):
    if job['chat_id'] != config.chat_id or job['user_id'] != config.user_id:
        raise ValueError('output_image_not_owned')
    total = store.conn.execute('SELECT coalesce(sum(length(photo)),0) FROM outbox').fetchone()[0]
    queued, rejected = 0, 0
    for index, photo in enumerate(prepared):
        dedupe = f'{key}:photo:{index}'
        if store.conn.execute('SELECT 1 FROM outbox WHERE dedupe=?', (dedupe,)).fetchone():
            continue
        if total + len(photo['data']) > MAX_STORED:
            rejected += 1
            continue
        caption = f"작업 #{job['job_id']} · {photo['caption'] or '이미지'}"
        store.conn.execute('INSERT INTO outbox(chat_id,body,dedupe,parent_id,photo,photo_mime,photo_sha256) VALUES(?,?,?,?,?,?,?)',
            (job['chat_id'], caption, dedupe, parent_id, photo['data'], photo['mime'], photo['sha256']))
        total += len(photo['data'])
        queued += 1
    return queued, rejected
