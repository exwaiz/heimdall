"""Bounded, explicit Korean/English intents. Never executes instructions in data."""
import re

from .knowledge import canonical_id, clean, title_suffix

HELP = ('/memory 저장해줘: 기억할 내용\n'
        '/memory GB-000001 읽어줘\n'
        '/memory 알림 주기 검색해줘\n'
        '/memory GB-000001 내용을 수정해줘: 새 전체 내용\n'
        'Heimdall 지식만 처리합니다. Codex 자체 메모리 설정은 바꾸지 않습니다.')
ID = re.compile(r'\b(?:GB|HK)-[0-9]{1,10}\b', re.I)
ACTION = re.compile(
    r'(?P<edit>(?:업데이트|수정|변경)(?=\s*(?:해|하자|[:：.!?]|$))|고쳐|바꿔|\bupdate\b|\bedit\b|\breplace\b)|'
    r'(?P<save>(?:저장|기억)(?=\s*(?:해|하자|[:：.!?]|$))|\bsave\b|\bremember\b)|'
    r'(?P<read>읽어|읽기|보여|조회|검색|찾아|알려|목록|리스트|\bread\b|\bshow\b|\bsearch\b|\blist\b)', re.I)
TAIL = re.compile(r'^\s*(?:해\s*주세요|해\s*줘|해두세요|해줄래|해둬|해주세요|해줘|하자|해|주세요|줘|두세요|둬|줄래)?[.!?。\s:：,-]*', re.I)


def parse(text):
    text = clean(text, 20500).strip()
    if re.search(r'(?:하지(?:는|도)?\s*(?:마|말|않)|안\s*(?:저장|기억|수정|변경)|지\s*말아|지\s*마|취소|삭제|잊어|forget|delete|do\s+not|don.t)', text, re.I):
        raise ValueError('저장·수정을 실행하지 않았습니다. 삭제는 /forget <ID> 를 사용하세요.\n' + HELP)
    ids = list(dict.fromkeys(canonical_id(m[0]) for m in ID.finditer(text)))
    match = ACTION.search(text)
    if not match:
        if len(ids) == 1 and ID.fullmatch(text):
            return 'read', ids[0], ''
        raise ValueError('요청이 명확하지 않아 변경하지 않았습니다.\n' + HELP)
    action = match.lastgroup
    before, after = text[:match.start()].strip(), TAIL.sub('', text[match.end():], count=1).strip()
    # A leading verb may follow only its ID and object marker, not other facts.
    prefix = ID.sub('', before).strip()
    prefix = re.sub(r'^(?:(?:의\s*)?(?:내용|정보|메모|기록|자료)(?:을|를|은|는)?|을|를)?\s*$', '', prefix)
    if action == 'read':
        if len(ids) > 1:
            raise ValueError('한 번에 자료 ID 하나만 지정하세요.')
        query = '' if match[0] in {'목록', '리스트', 'list'} else (before + ' ' + after).strip(' .!?：:')
        query = re.sub(r'^(?:내용|메모리|자료|기록)(?:을|를)?\s*$', '', query)
        page_match = re.search(r'(\d+)\s*페이지', query)
        page = int(page_match[1]) if page_match else 1
        scope = re.sub(r'\d+\s*페이지', '', query).strip()
        # Only whole collection phrases qualify: "TST731 모든 기록" is
        # still a topic search, not permission to disclose every record.
        collection = re.fullmatch(
            r'(?:(?:저장된|저장한|저장되어\s*있는|내가\s*저장한)\s*)?'
            r'(?:(?:모든|전체|전부|모두|다)\s*)?'
            r'(?:(?:지식|메모리|메모|기록|자료|내용|정보)(?:들)?(?:을|를|은|는)?\s*)?'
            r'(?:(?:모든|전체|전부|모두|다)(?:를|을)?\s*)?', scope)
        if not ids and (collection is not None or match[0] in {'목록', '리스트', 'list'}):
            if not 1 <= page <= 2000:
                raise ValueError('페이지는 1~2000입니다.')
            return 'all', None, str(page)
        return action, ids[0] if ids else None, query
    if action == 'edit' and len(ids) != 1:
        raise ValueError('수정할 자료 ID 하나를 지정하세요.\n' + HELP)
    if action == 'save' and ids:
        raise ValueError('기존 자료 수정은 ID와 함께 수정해줘라고 요청하세요.\n' + HELP)
    if not prefix:
        body = after
    elif not after:
        body = ID.sub('', before).strip()
        if action == 'edit' and re.search(r'(?:에서|을|를).{0,50}(?:으로|로)\s*$', body):
            raise ValueError('부분 변경을 추측하지 않습니다. 새 전체 내용을 적어주세요.\n' + HELP)
        body = re.sub(r'^(?:의\s*)?(?:내용|정보|메모|기록|자료)(?:을|를)?\s*[:：]\s*', '', body)
        body = re.sub(r'(?:으로|로)\s*$', '', body)
    else:
        raise ValueError('변경 범위가 애매합니다. 명령 뒤에 저장할 새 전체 내용을 적어주세요.\n' + HELP)
    if not body or (action == 'edit' and re.search(r'(?:에서|을|를).{0,50}(?:으로|로)\s*$', body)):
        raise ValueError('부분 변경을 추측하지 않습니다. 새 전체 내용을 적어주세요.\n' + HELP)
    return action, ids[0] if ids else None, clean(body, 20000)


def handle_memory(knowledge, text, chat, user, message_id):
    knowledge.authorize(chat, user)
    action, target, body = parse(text)
    if action == 'all':
        page, size = int(body), 5
        conn = knowledge.store.conn
        total = conn.execute('SELECT count(*) FROM items WHERE chat_id=? AND user_id=? AND deleted_at IS NULL',
                             (chat, user)).fetchone()[0]
        if not total:
            return '저장된 지식이 없습니다.'
        pages = (total + size - 1) // size
        if page > pages:
            return f'총 {total}개 · 마지막 페이지는 {pages}페이지입니다.'
        rows = conn.execute('SELECT item_id FROM items WHERE chat_id=? AND user_id=? AND deleted_at IS NULL ORDER BY item_id LIMIT ? OFFSET ?',
                            (chat, user, size, (page - 1) * size)).fetchall()
        blocks = [f'저장된 전체 지식 {total}개 · {page}/{pages}페이지 (페이지당 최대 {size}개)']
        for row in rows:
            item = knowledge.get(row[0], chat, user)
            content = '\n'.join(c['text'] for c in item['contents'])
            blocks.append(f"{item['id']} · 수정 {item['revision']}{title_suffix(item, content)}\n" +
                          (content[:3000] or '(저장된 본문 없음)') +
                          (f"\n[긴 본문 일부 표시 · /item {item['id']} 로 개별 조회]" if len(content) > 3000 else ''))
        if page < pages:
            blocks.append(f'다음: /memory 전체 읽어줘 {page + 1}페이지')
        return '\n\n'.join(blocks)
    if action == 'read':
        hits = ([knowledge.get(target, chat, user)] if target else
                knowledge.search(body, chat, user) if body else knowledge.recent(chat, user))
        blocks = []
        for item in hits:
            content = '\n'.join(c['text'] for c in item['contents'])
            blocks.append(f"{item['id']} · 수정 {item['revision']}{title_suffix(item, content)}\n" + content[:8000])
        return '\n\n'.join(blocks) or '기록에서 찾지 못했습니다.'
    if type(message_id) is not int or message_id <= 0:
        raise ValueError('메시지 ID가 필요합니다.')
    if action == 'save':
        with knowledge.store.transaction():
            value = knowledge.save(chat, user, message_id, body)
        return f'{value} · Heimdall 메모 저장 완료.\n' + body[:3500]
    value, revision = knowledge.edit(target, chat, user, message_id, body)
    return (f'{value} · 수정 {revision} 완료. 검색에 사용할 내용을 교체했습니다. '
            '수정 전 기록·첨부 원본은 보존했습니다.\n' + body[:3500])
