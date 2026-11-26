from jurialmunkey.ftools import cached_property
from tmdbhelper.lib.items.database.basedata import ItemDetailsDatabaseAccess


class ItemDetailsList(ItemDetailsDatabaseAccess):
    conflict_constraint = 'id'
    conditions = 'parent_id=?'  # WHERE conditions
    keys = ()

    @property
    def values(self):  # WHERE conditions values for ?
        return (self.parent_id, )

    @property
    def cached_data_table(self):
        return self.table

    @property
    def cached_data_keys(self):
        return self.keys

    def get_cached_data(self):
        return self.get_cached_list_values(self.cached_data_table, self.cached_data_keys, self.values, self.conditions)

    @cached_property
    def cached_data(self):
        return self.get_cached_data()

    def get_configure_mapped_data_list(self, i, k):
        v = i.get(k)
        if not v and k == 'parent_id':
            return self.item_id
        return v

    def configure_mapped_data_list(self, data):
        return [tuple([self.get_configure_mapped_data_list(i, k) for k in self.keys]) for i in data[self.table]]

    def try_cached_data(self, online_data_mapped):
        kwgs = {'conflict_constraint': self.conflict_constraint}
        args = (self.table, self.keys, self.configure_mapped_data_list(online_data_mapped))
        return (self.set_or_update_null_cached_list_values, args, kwgs)
        # args = (self.table, self.keys, self.configure_mapped_data_list(online_data_mapped))
        # return (self.set_cached_list_values, args, {})


class ArtworkDetailsMixin:
    has_iso_country = True

    @staticmethod
    def image_path_func(v):
        return v

    def get_cached_data_by_iso_country(self):
        conditions = f'iso_country=? AND {self.conditions}'
        values = (self.common_apis.tmdb_api.iso_country, *self.values)
        return self.get_cached_list_values(self.cached_data_table, self.cached_data_keys, values, conditions)

    def get_cached_data_by_iso_language(self):
        conditions = f'iso_language=? AND {self.conditions}'
        values = (self.common_apis.tmdb_api.iso_language, *self.values)
        return self.get_cached_list_values(self.cached_data_table, self.cached_data_keys, values, conditions)

    def get_cached_data_by_language(self):
        return self.get_cached_data_by_iso_country() or self.get_cached_data_by_iso_language()

    def get_cached_data_by_english(self):
        conditions = f'iso_language=? AND {self.conditions}'
        values = ('en', *self.values)
        return self.get_cached_list_values(self.cached_data_table, self.cached_data_keys, values, conditions)

    def get_cached_data_by_null(self):
        conditions = f'(iso_language IS NULL OR iso_language="xx") AND {self.conditions}'
        return self.get_cached_list_values(self.cached_data_table, self.cached_data_keys, self.values, conditions)

    def _get_cached_data_unified(self, include_null=True):
        base_cond, _, tail = self.conditions.partition(' ORDER BY ')
        order_part, _, limit_part = tail.partition(' LIMIT ')
        iso_l = self.common_apis.tmdb_api.iso_language
        null_clause = ' OR iso_language IS NULL OR iso_language="xx"' if include_null else ''
        if self.has_iso_country:
            iso_c = self.common_apis.tmdb_api.iso_country
            conditions = f'(iso_country=? OR iso_language=? OR iso_language="en"{null_clause}) AND ({base_cond})'
            priority_expr = (
                'CASE '
                'WHEN iso_country=? THEN 0 '
                'WHEN iso_language=? THEN 1 '
                'WHEN iso_language="en" THEN 2 '
                'ELSE 3 END'
            )
            values = (iso_c, iso_l, *self.values, iso_c, iso_l)
        else:
            conditions = f'(iso_language=? OR iso_language="en"{null_clause}) AND ({base_cond})'
            priority_expr = (
                'CASE '
                'WHEN iso_language=? THEN 0 '
                'WHEN iso_language="en" THEN 1 '
                'ELSE 2 END'
            )
            values = (iso_l, *self.values, iso_l)
        if order_part:
            conditions += f' ORDER BY {priority_expr}, {order_part}'
        else:
            conditions += f' ORDER BY {priority_expr}'
        if limit_part and 'OFFSET' not in limit_part.upper():
            conditions += f' LIMIT {limit_part}'
        return self.get_cached_list_values(self.cached_data_table, self.cached_data_keys, values, conditions)

    def get_cached_data(self):
        if 'OFFSET' in self.conditions.upper():
            return self.get_cached_data_by_language() or self.get_cached_data_by_english() or self.get_cached_data_by_null()
        return self._get_cached_data_unified(include_null=True)
