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
        'art': {},
        'art_ids': set(),
        'default_art': set(),
        'fanart_tv': {},
        'fanart_tv_ids': set(),
        'art_ctx': (common_apis := dbs[0].common_apis).tmdb_api.iso_country,
        'art_lang': common_apis.tmdb_api.iso_language,
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
    iso_country = batch['art_ctx']
    iso_language = batch['art_lang']
    batch['cert_ctx'] = iso_country
    batch['video_ctx'] = ('Trailer', iso_language)

    all_pid_set = set(all_parent_ids)
    all_iid_set = set(all_item_ids)
    has_movie = any(getattr(d, 'mediatype', None) == 'movie' for d in dbs)
    has_tv = any(getattr(d, 'mediatype', None) in ('tvshow', 'season', 'episode') for d in dbs)
    has_ftv = any(getattr(d, 'fanart_tv', 0) for d in dbs)
    is_extended = any(getattr(d, 'extendedinfo', False) for d in dbs)
    batch['crew_widget_only'] = not is_extended

    # Preload user_art existence check once per batch connection
    if not hasattr(connection, '_has_user_art_cache'):
        try:
            connection._has_user_art_cache = bool(cursor.execute('SELECT 1 FROM user_art LIMIT 1').fetchone())
        except Exception:
            connection._has_user_art_cache = False

    # Preload art (forced parent_id index scan) and default_art separately (avoids per-row correlated subquery & MULTI-INDEX OR)
    art_rows = _exec_in_chunks(
        cursor,
        'SELECT aspect_ratio, quality, iso_language, iso_country, icon, type, extension, rating, votes, parent_id '
        'FROM art WHERE parent_id IN ({ph}) '
        'AND CASE WHEN iso_country=? OR iso_language=? OR iso_language IN ("en", "xx") OR iso_language IS NULL THEN 1 ELSE 0 END = 1',
        all_item_ids,
        suffix_params=(iso_country, iso_language),
    )
    def_rows = _exec_in_chunks(
        cursor,
        'SELECT parent_id, type, icon FROM default_art WHERE parent_id IN ({ph})',
        all_item_ids,
    )
    batch['art_ids'] = all_iid_set
    batch['art'] = _group_by(art_rows, 'parent_id', limit_per_key=None)
    batch['default_art'] = {(r['parent_id'], r['type'], r['icon']) for r in def_rows}

    if has_ftv:
        ftv_rows = _exec_in_chunks(
            cursor,
            'SELECT icon, iso_language, likes, quality, type, extension, parent_id '
            'FROM fanart_tv WHERE parent_id IN ({ph}) '
            'AND CASE WHEN iso_language=? OR iso_language IN ("en", "xx") OR iso_language IS NULL THEN 1 ELSE 0 END = 1',
            all_item_ids,
            suffix_params=(iso_language,),
        )
        batch['fanart_tv_ids'] = all_iid_set
        batch['fanart_tv'] = _group_by(ftv_rows, 'parent_id', limit_per_key=None)

    # Collect unique collection_ids from preloaded movie rows
    collection_ids = []
    seen_coll = set()
    for (cls, _), bi_rows in batch['baseitem'].items():
        for r in bi_rows:
            try:
                cid = r['collection_id']
            except (IndexError, KeyError):
                cid = None
            if cid and cid not in seen_coll:
                seen_coll.add(cid)
                collection_ids.append(cid)

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
        ('translation', all_item_ids, all_iid_set,
         'SELECT iso_country, iso_language, plot, title, tagline, parent_id FROM translation WHERE parent_id IN ({ph})', (), (), None),
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
    if getattr(connection, '_has_user_art_cache', False):
        queries.append(
            ('user_art', all_item_ids, all_iid_set,
             'SELECT type, icon, parent_id FROM user_art WHERE parent_id IN ({ph}) AND icon IS NOT NULL', (), (), None)
        )
    if collection_ids:
        coll_set = set(collection_ids)
        queries.append(
            ('series_stats', collection_ids, coll_set,
             'SELECT belongs.parent_id AS parent_id, '
             'ROUND(AVG(CASE WHEN rating > 0 THEN rating ELSE NULL END), 1) as rating, '
             'SUM(votes) as votes, COUNT(movie.tmdb_id) as numitems, '
             'MAX(year) as year_last, MIN(year) as year_first '
             'FROM movie INNER JOIN belongs ON movie.id = belongs.id '
             'WHERE belongs.parent_id IN ({ph}) GROUP BY belongs.parent_id', (), (), 1)
        )
        queries.append(
            ('series_genre', collection_ids, coll_set,
             'SELECT DISTINCT belongs.parent_id AS parent_id, name, tmdb_id '
             'FROM genre INNER JOIN belongs ON genre.parent_id = belongs.id '
             'WHERE belongs.parent_id IN ({ph}) ORDER BY name', (), (), None)
        )
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

    # Series collection joins
    if table == 'movie INNER JOIN belongs ON movie.id = belongs.id' and 'GROUP BY belongs.parent_id' in getattr(meta_obj, 'conditions', ''):
        tids = batch['table_ids'].get('series_stats')
        if not tids:
            return None
        pid = meta_obj.values[0] if meta_obj.values else None
        return batch['tables'].get('series_stats', {}).get(pid, []) if pid in tids else None

    if table == 'genre INNER JOIN belongs ON genre.parent_id = belongs.id':
        tids = batch['table_ids'].get('series_genre')
        if not tids:
            return None
        pid = meta_obj.values[0] if meta_obj.values else None
        return batch['tables'].get('series_genre', {}).get(pid, []) if pid in tids else None

    # Simple / joined parent_id tables
    tids = batch['table_ids'].get(table)
    if not tids:
        return None

    pid = meta_obj.values[0] if meta_obj.values else None
    if pid not in tids:
        return None

    rows = batch['tables'].get(table, {}).get(pid, [])

    if table == 'translation':
        if meta_obj.conditions == 'parent_id=? AND iso_language="en" LIMIT 1':
            for r in rows:
                if r['iso_language'] == 'en':
                    return [r]
            return []
        if meta_obj.conditions == 'parent_id=?':
            return rows
        return None

    if table == 'user_art':
        vals = meta_obj.values
        if len(vals) >= 2 and meta_obj.conditions == 'parent_id=? AND type=? AND icon IS NOT NULL LIMIT 1':
            t = vals[1]
            for r in rows:
                if r['type'] == t:
                    return [r]
            return []
        return None

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
    batch = _get_batch(getattr(art_obj, 'connection', None))
    if not batch:
        return None
    table = getattr(art_obj, 'table', None)
    if table not in ('art', 'fanart_tv'):
        return None
    vals = art_obj.values
    if not vals:
        return None
    pid = vals[0]
    id_set = batch['art_ids'] if table == 'art' else batch['fanart_tv_ids']
    if pid not in id_set:
        return None
    rows = (batch['art'] if table == 'art' else batch['fanart_tv']).get(pid)
    if not rows:
        return []

    cond = art_obj.conditions or ''
    if 'type=? AND extension=?' in cond and len(vals) >= 3:
        t1, ext = vals[1], vals[2]
        type_rows = [r for r in rows if r['type'] == t1 and r['extension'] == ext]
    elif '(type=? OR type=?)' in cond and len(vals) >= 3:
        t1, t2 = vals[1], vals[2]
        type_rows = [r for r in rows if r['type'] == t1 or r['type'] == t2]
    elif 'type=?' in cond and len(vals) >= 2:
        t1 = vals[1]
        type_rows = [r for r in rows if r['type'] == t1]
    else:
        return None

    if not type_rows:
        return []

    iso_c = batch['art_ctx']
    iso_l = batch['art_lang']
    has_c = bool(getattr(art_obj, 'has_iso_country', False)) and table == 'art'

    if mode == 'iso_country':
        if not has_c:
            return []
        matched = [(0, r) for r in type_rows if r['iso_country'] == iso_c]
    elif mode == 'iso_language':
        matched = [(0, r) for r in type_rows if r['iso_language'] == iso_l]
    elif mode == 'english':
        matched = [(0, r) for r in type_rows if r['iso_language'] == 'en']
    elif mode == 'null':
        matched = [(0, r) for r in type_rows if r['iso_language'] is None or r['iso_language'] == 'xx']
    elif mode == 'unified':
        matched = []
        if has_c:
            for r in type_rows:
                rc = r['iso_country']
                rl = r['iso_language']
                if rc == iso_c:
                    matched.append((0, r))
                elif rl == iso_l:
                    matched.append((1, r))
                elif rl == 'en':
                    matched.append((2, r))
                elif include_null and (rl is None or rl == 'xx'):
                    matched.append((3, r))
        else:
            for r in type_rows:
                rl = r['iso_language']
                if rl == iso_l:
                    matched.append((0, r))
                elif rl == 'en':
                    matched.append((1, r))
                elif include_null and (rl is None or rl == 'xx'):
                    matched.append((2, r))
    else:
        return None

    if not matched:
        return []

    if table == 'art':
        if 'isdefault DESC' in cond:
            def_set = batch['default_art']
            keyfn = lambda item: (item[0], 0 if (pid, item[1]['type'], item[1]['icon']) in def_set else 1, item[1]['rating'] is None, -(item[1]['rating'] or 0))
        else:
            keyfn = lambda item: (item[0], item[1]['rating'] is None, -(item[1]['rating'] or 0))
    else:
        keyfn = lambda item: (item[0], item[1]['likes'] is None, -(item[1]['likes'] or 0))

    if 'LIMIT 10 OFFSET 1' in cond:
        matched.sort(key=keyfn)
        return [item[1] for item in matched[1:11]]
    if 'LIMIT 1' in cond:
        return [min(matched, key=keyfn)[1]]
    matched.sort(key=keyfn)
    return [item[1] for item in matched]
