from collections import defaultdict

_CHUNK_SIZE = 200


def _chunks(seq, size=_CHUNK_SIZE):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _get_batch(connection):
    if not connection:
        return None
    return getattr(connection, '_batch', None)


def _exec_in_chunks(cursor, sql_template, ids, prefix_params=(), suffix_params=()):
    rows = []
    for chunk in _chunks(ids):
        ph = ','.join('?' * len(chunk))
        sql = sql_template.format(ph=ph)
        res = cursor.execute(sql, (*prefix_params, *chunk, *suffix_params))
        if res:
            rows.extend(res.fetchall())
    return rows


def _group_by(rows, key='parent_id', limit_per_key=None):
    grouped = defaultdict(list)
    for row in rows:
        pid = row[key]
        if limit_per_key is not None and len(grouped[pid]) >= limit_per_key:
            continue
        grouped[pid].append(row)
    return grouped


def preload_batch(connection, listitem_configs):
    """Preload ItemDetails.db rows for a widget batch using WHERE ... IN (...) queries."""
    cursor = getattr(connection, 'open_connection', None)
    if cursor is None or not listitem_configs:
        return

    dbs = []
    for lc in listitem_configs:
        cacher = getattr(lc, 'listitem_cacher', None)
        db_cache = getattr(cacher, 'baseitem_db_cache', None) if cacher else None
        if db_cache is not None and db_cache.data_cond:
            db_cache.connection = connection
            db_cache.cache_refresh = lc.parent.cache_refresh
            db_cache.cache_translations = lc.parent.cache_translations
            dbs.append(db_cache)

    if len(dbs) < 2:
        return

    batch = {
        'baseitem': {},
        'baseitem_ids': {},
        'simplecache': {},
        'simplecache_ids': set(),
        'tables': {},
        'table_ids': {},
        'cert_ctx': None,
        'video_ctx': None,
        'crew_widget_only': False,
    }

    # 1. BaseItem batch grouped by (class, language, datalevel, fanart_tv, translation, cache_refresh)
    by_bi_group = defaultdict(list)
    all_item_ids = []
    all_parent_ids = []
    seen_item = set()
    seen_parent = set()

    for db in dbs:
        iid = db.item_id
        pid = db.parent_id
        if iid not in seen_item:
            seen_item.add(iid)
            all_item_ids.append(iid)
        if pid not in seen_parent:
            seen_parent.add(pid)
            all_parent_ids.append(pid)
        if iid != pid and pid not in seen_item:
            seen_item.add(pid)
            all_item_ids.append(pid)
        gkey = (
            type(db),
            db.language,
            db.datalevel,
            db.fanart_tv,
            db.translation,
            db.cache_refresh == 'never',
        )
        by_bi_group[gkey].append(db)

    for gkey, group_dbs in by_bi_group.items():
        cls = gkey[0]
        sample = group_dbs[0]
        g_ids = list(dict.fromkeys(d.item_id for d in group_dbs))
        # Replace leading baseitem.id=? with baseitem.id IN ({ph})
        conds = sample.cached_data_conditions
        if not conds.startswith('baseitem.id=?'):
            continue
        tail_cond = conds[len('baseitem.id=?'):]
        tail_vals = sample.cached_data_values[1:]
        keys_sql = ', '.join(('baseitem.id AS _batch_item_id', *sample.cached_data_keys))
        sql_tpl = f'SELECT {keys_sql} FROM {sample.cached_data_table} WHERE baseitem.id IN ({{ph}}){tail_cond}'
        rows = _exec_in_chunks(cursor, sql_tpl, g_ids, suffix_params=tail_vals)
        id_set = batch['baseitem_ids'].setdefault(cls, set())
        id_set.update(g_ids)
        for row in rows:
            batch['baseitem'].setdefault((cls, row['_batch_item_id']), []).append(row)

    # 2. simplecache (Trakt plays, progress, ranks, lastplayed)
    sc_rows = _exec_in_chunks(
        cursor,
        'SELECT id, plays, watched_episodes, aired_episodes, playback_progress, '
        'favorites_rank, watchlist_rank, collection_last_collected_at, '
        'last_watched_at, playback_paused_at FROM simplecache WHERE id IN ({ph})',
        all_item_ids,
    )
    batch['simplecache_ids'] = set(all_item_ids)
    for row in sc_rows:
        batch['simplecache'][row['id']] = row

    # 3. Subtables queried per item in ItemMetaFactory
    common_apis = dbs[0].common_apis
    iso_country = common_apis.tmdb_api.iso_country
    iso_language = common_apis.tmdb_api.iso_language
    batch['cert_ctx'] = iso_country
    batch['video_ctx'] = ('Trailer', iso_language)

    all_pid_set = set(all_parent_ids)
    all_iid_set = set(all_item_ids)
    has_movie = any(getattr(d, 'mediatype', None) == 'movie' for d in dbs)
    has_tv = any(getattr(d, 'mediatype', None) in ('tvshow', 'season', 'episode') for d in dbs)
    is_extended = any(getattr(d, 'extendedinfo', False) for d in dbs)
    batch['crew_widget_only'] = not is_extended

    crew_sql = (
        'SELECT crewmember.parent_id AS parent_id, crewmember.tmdb_id, role, department, appearances, '
        'name, gender, biography, known_for_department, '
        '(SELECT art.icon FROM art WHERE art.parent_id=\'person.\' || crewmember.tmdb_id AND art.type=\'profiles\' '
        'ORDER BY art.rating DESC LIMIT 1) as thumb '
        'FROM crewmember INNER JOIN person ON person.tmdb_id = crewmember.tmdb_id '
        'WHERE crewmember.parent_id IN ({ph})'
        + ('' if is_extended else ' AND (department="Writing" OR (department="Directing" AND role="Director") OR role="Creator")')
        + ' ORDER BY appearances DESC'
    )

    queries = [
        ('genre', all_parent_ids, all_pid_set,
         'SELECT name, tmdb_id, parent_id FROM genre WHERE parent_id IN ({ph})', (), (), None),
        ('unique_id', all_item_ids, all_iid_set,
         'SELECT key, value, parent_id FROM unique_id WHERE parent_id IN ({ph})', (), (), None),
        ('custom', all_item_ids, all_iid_set,
         'SELECT key, value, parent_id FROM custom WHERE parent_id IN ({ph})', (), (), None),
        ('certification', all_parent_ids, all_pid_set,
         'SELECT name, iso_country, iso_language, release_date, release_type, parent_id '
         'FROM certification WHERE parent_id IN ({ph}) AND iso_country=? AND name IS NOT NULL AND name != ""',
         (), (iso_country,), 1),
        ('video', all_parent_ids, all_pid_set,
         'SELECT name, iso_country, iso_language, release_date, key, path, content, parent_id '
         'FROM video WHERE parent_id IN ({ph}) AND content=? ORDER BY iso_language=? DESC, release_date DESC',
         (), ('Trailer', iso_language), 1),
        ('country', all_parent_ids, all_pid_set,
         'SELECT countries.name, countries.iso_country, country.parent_id AS parent_id '
         'FROM country INNER JOIN countries ON countries.iso_country = country.iso_country '
         'WHERE country.parent_id IN ({ph})', (), (), None),
        ('castmember', all_item_ids, all_iid_set,
         'SELECT castmember.parent_id AS parent_id, castmember.tmdb_id, GROUP_CONCAT(role, " / ") as role, '
         'ordering, appearances, guest, name, gender, biography, known_for_department, '
         '(SELECT art.icon FROM art WHERE art.parent_id=\'person.\' || castmember.tmdb_id AND art.type=\'profiles\' '
         'ORDER BY art.rating DESC LIMIT 1) as thumb '
         'FROM castmember INNER JOIN person ON person.tmdb_id = castmember.tmdb_id '
         'WHERE castmember.parent_id IN ({ph}) GROUP BY castmember.parent_id, castmember.tmdb_id '
         'ORDER BY IFNULL(ordering, 9999) ASC', (), (), 100),
        ('crewmember', all_item_ids, all_iid_set, crew_sql, (), (), None),
    ]
    if has_movie:
        queries.append(
            ('studio', all_parent_ids, all_pid_set,
             'SELECT company.name, company.tmdb_id, company.logo, company.country, studio.parent_id AS parent_id '
             'FROM studio INNER JOIN company ON company.tmdb_id = studio.tmdb_id '
             'WHERE studio.parent_id IN ({ph})', (), (), None)
        )
    if has_tv:
        queries.append(
            ('network', all_parent_ids, all_pid_set,
             'SELECT broadcaster.name, broadcaster.tmdb_id, broadcaster.logo, broadcaster.country, network.parent_id AS parent_id '
             'FROM network INNER JOIN broadcaster ON broadcaster.tmdb_id = network.tmdb_id '
             'WHERE network.parent_id IN ({ph})', (), (), None)
        )

    for tname, id_list, id_set, sql_tpl, prefix_p, suffix_p, lim in queries:
        rows = _exec_in_chunks(cursor, sql_tpl, id_list, prefix_p, suffix_p)
        if tname == 'certification' and rows:
            rows.sort(key=lambda r: r['release_date'] or '9999-99-99')
        batch['table_ids'][tname] = id_set
        batch['tables'][tname] = _group_by(rows, 'parent_id', limit_per_key=lim)

    connection._batch = batch


def batch_lookup_baseitem(baseitem_obj):
    batch = _get_batch(getattr(baseitem_obj, 'connection', None))
    if not batch:
        return None
    cls = type(baseitem_obj)
    ids = batch['baseitem_ids'].get(cls)
    if not ids or baseitem_obj.item_id not in ids:
        return None
    return batch['baseitem'].get((cls, baseitem_obj.item_id), [])


def batch_lookup_list(meta_obj):
    batch = _get_batch(getattr(meta_obj, 'connection', None))
    if not batch:
        return None
    table = getattr(meta_obj, 'table', None)

    # simplecache (Trakt)
    if table == 'simplecache':
        iid = meta_obj.item_id
        if iid not in batch['simplecache_ids']:
            return None
        row = batch['simplecache'].get(iid)
        if not row:
            return []
        if meta_obj.keys == ('last_watched_at', 'playback_paused_at'):
            lw = row['last_watched_at'] or ''
            pp = row['playback_paused_at'] or ''
            return [{'lastplayed': lw if lw >= pp else pp}]
        return [row]

    # Simple / joined parent_id tables
    tids = batch['table_ids'].get(table)
    if not tids:
        return None

    pid = meta_obj.values[0] if meta_obj.values else None
    if pid not in tids:
        return None

    rows = batch['tables'].get(table, {}).get(pid, [])

    if table == 'unique_id':
        if meta_obj.conditions == 'parent_id=? AND key="imdb"':
            return [r for r in rows if r['key'] == 'imdb']
        if meta_obj.conditions == 'parent_id=?':
            return rows
        return None

    if table == 'certification':
        if len(meta_obj.values) >= 2 and meta_obj.values[1] == batch['cert_ctx']:
            return rows
        return None

    if table == 'video':
        if len(meta_obj.values) >= 3 and (meta_obj.values[1], meta_obj.values[2]) == batch['video_ctx']:
            return rows
        return None

    if table == 'crewmember':
        cond = meta_obj.conditions
        vals = meta_obj.values
        if cond == 'parent_id=? ORDER BY appearances DESC LIMIT 100':
            if batch.get('crew_widget_only'):
                return None
            return rows[:100]
        if cond == 'parent_id=? AND role=? LIMIT 20' and len(vals) >= 2:
            if batch.get('crew_widget_only') and vals[1] != 'Creator':
                return None
            return [r for r in rows if r['role'] == vals[1]][:20]
        if cond == 'parent_id=? AND department=? LIMIT 20' and len(vals) >= 2:
            if batch.get('crew_widget_only') and vals[1] != 'Writing':
                return None
            return [r for r in rows if r['department'] == vals[1]][:20]
        if cond == 'parent_id=? AND department=? AND role=? LIMIT 20' and len(vals) >= 3:
            if batch.get('crew_widget_only') and not (vals[1] == 'Directing' and vals[2] == 'Director'):
                return None
            return [r for r in rows if r['department'] == vals[1] and r['role'] == vals[2]][:20]
        return None

    if table in ('genre', 'custom', 'country', 'studio', 'network', 'castmember'):
        return rows

    return None


def batch_lookup_art(art_obj, mode='unified', include_null=True):
    return None
