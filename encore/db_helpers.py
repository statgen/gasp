import re
import MySQLdb
from collections import namedtuple
from math import ceil

QueryInfo = namedtuple('QueryInfo', ['page', 'order_by', 'filter'], verbose=False)

class OrderClause:
    def __init__(self, *exprs):
        self.order_by = []
        for expr in exprs:
            self.add(expr)

    def add(self, column_order):
        if not isinstance(column_order, OrderExpression):
            raise TypeError("OrderClause expects an OrderExpression object")
        self.order_by.append(column_order)

    def to_clause(self):
        if len(self.order_by)==0:
            return ""
        return "ORDER BY " + ", ".join((x.to_clause() for x in self.order_by))

class OrderExpression:
    def __init__(self, column, direction="ASC"):
        self.column = column
        self.direction = direction

    def to_clause(self):
        return "{} {}".format(self.column, self.direction)

    def __repr__(self):
        return "<OrderExpression {},{}>".format(self.column, self.direction)

class TableJoin:
    def __init__(self, table, on, join_type="LEFT"):
        self.table = table
        self.on = on
        self.join_type = "LEFT"

    def to_clause(self):
        return "{} JOIN {} ON {}".format(self.join_type, self.table, self.on)


class WhereExpression:
    def __init__(self, where="", vals=()):
        self.where = where
        self.vals = vals

    def to_clause(self):
        return self.where, self.vals

class WhereGroup:
    def __init__(self, *exprs):
        self.__join_verb = "??"
        self.wheres = []
        if len(exprs)==1 and isinstance(exprs[0], WhereGroup):
            self.wheres = exprs[0].wheres
            self.__join_verb = exprs[0].__join_verb
        for expr in exprs:
            self.add(expr)

    def add(self, expr):
        if expr is None:
            return
        if not isinstance(expr, WhereExpression) and not isinstance(expr, WhereGroup):
            raise TypeError("Where expects a WhereExpression object or group")
        self.wheres.append(expr)

    def to_clause(self):
        if len(self.wheres) <1 :
            return None, None
        elif len(self.wheres) == 1:
            return self.wheres[0].to_clause()
        where = []
        vals = ()
        for expr in self.wheres:
            w, v = expr.to_clause()
            where.append(w)
            vals = vals + v
        connect = ") " + self.__join_verb + " ("
        wheres = "(" + connect.join(where) + ")"
        return wheres, vals

class WhereAll(WhereGroup):
    def __init__(self, *args):
        super().__init__(*args)
        self.__join_verb = "AND"

class WhereAny(WhereGroup):
    def __init__(self, *args):
        super().__init__(*args)
        self.__join_verb = "OR"

class WhereClause(WhereAll):
    def __init__(self, *args):
        super().__init__(*args)

    def to_clause(self):
        w, v = super().to_clause()
        if not w:
            return "", ()
        return "WHERE " + w, v

class SelectQuery:
    def __init__(self):
        self.cols = []
        self.table = ""
        self.joins = []
        self.where = None
        self.qfilter = None
        self.order = None
        self.page = None

    @staticmethod
    def __base_sql(cols=[], table="", joins=[], where=None, order=None, page=None):
        vals = ()
        sql = "SELECT "
        sql += ", ".join(cols)
        sql += " FROM " + table
        for join in joins:
            sql += " " + join.to_clause()
        if where:
            w, v =  where.to_clause()
            sql += " " + w
            vals += v
        if order:
            sql += " " + order.to_clause()
        if page:
            sql += " LIMIT %s OFFSET %s"
            vals += (page.limit, page.offset)
        return sql, vals

    def cmd_select(self):
        sql, vals = SelectQuery.__base_sql(self.cols, self.table, self.joins,
            self.__to_where_clause(self.where, self.qfilter), self.order, self.page)
        return sql, vals

    def cmd_count(self):
        sql, vals = SelectQuery.__base_sql(["count(*) as count"], self.table, self.joins,
            self.__to_where_clause(self.where, self.qfilter))
        return sql, vals

    def cmd_count_unfiltered(self):
        sql, vals = SelectQuery.__base_sql(["count(*) as count"], self.table, self.joins,
            self.__to_where_clause(self.where))
        return sql, vals

    def set_cols(self, cols):
        self.cols = cols
        return self

    def add_col(self, col):
        self.cols.append(col)
        return self

    def set_table(self, table):
        self.table = table
        return self

    def set_joins(self, joins):
        self.joins = joins
        return self

    def add_join(self, join):
        self.joins.append(join)
        return self

    def set_where(self, where):
        self.where = where
        return self

    def set_filter(self, qfilter):
        self.qfilter = qfilter
        return self

    def set_order_by(self, order):
        self.order = order
        return self

    def set_page(self, page):
        self.page = page
        return self

    @staticmethod
    def __to_where_clause(*args):
        if len(args) < 1:
            return None
        return WhereClause(*args)


PageInfo = namedtuple('PageInfo', ['limit', 'offset'], verbose=False)

class PagedResult:
    def __init__(self, results, total_count=0, filtered_count=0, page=None):
        self.results = results
        self.page = page
        self.total_count = total_count
        self.filtered_count = filtered_count

    def next_page(self):
        if self.page is None:
            return None
        if self.page.offset + self.page.limit >= self.total_count:
            return None
        return PageInfo(self.page.limit, self.page.offset + self.page.limit)

    def prev_page(self):
        if self.page is None:
            return None
        if self.page.offset == 0:
            return None
        return PageInfo(self.page.limit, min(self.page.offset-self.page.limit, 0))

    def page_count(self):
        if self.page is None:
            if self.total_count>0:
                return 1
            else:
                return 0
        return int(ceil(self.total_count / float(self.page.limit)))

    @staticmethod
    def execute_select(db, sqlcmd):
        cur = db.cursor(MySQLdb.cursors.DictCursor)
        sql, vals =  sqlcmd.cmd_select()
        page = sqlcmd.page
        qfilter = sqlcmd.qfilter
        cur.execute(sql, vals)
        results = cur.fetchall()
        if page and (page.offset>0 or len(results)==page.limit):
            sql, vals = sqlcmd.cmd_count()
            cur.execute(sql, vals)
            total_count = cur.fetchone()["count"]
        else:
            total_count = len(results)
        if qfilter:
            sql, vals = sqlcmd.cmd_count_unfiltered()
            cur.execute(sql, vals)
            filtered_count = total_count
            total_count = cur.fetchone()["count"]
        else:
            filtered_count = total_count
        return PagedResult(results, total_count, filtered_count, page)

class DBException(Exception):
    pass