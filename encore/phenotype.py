import os
import shutil
import json
from . import sql_pool
import MySQLdb
from collections import OrderedDict
from .pheno_reader import PhenoReader
from .db_helpers import SelectQuery, TableJoin, PagedResult, OrderClause, OrderExpression, WhereExpression, WhereAll

class Phenotype:
    __dbfields = ["name", "description", "orig_file_name", "md5sum",
        "user_id", "creation_date", "is_active"]

    def __init__(self, pheno_id, meta=None):
        self.pheno_id = pheno_id
        self.name = None
        self.description = None
        self.orig_file_name = None
        self.md5sum = None
        self.user_id = None
        self.creation_date = None
        self.root_path = "" 
        self.meta = meta
       
    def get_raw_path(self):
        return self.relative_path("pheno.txt")

    def get_pheno_reader(self):
        return PhenoReader(self.get_raw_path(), self.meta)

    def relative_path(self, *args):
        return os.path.expanduser(os.path.join(self.root_path, *args))

    def get_column_class(self, covar_name):
        covar = self.__get_column(covar_name)
        return covar.get("class", "")

    def get_column_type(self, covar_name):
        covar = self.__get_column(covar_name)
        return covar.get("type", "")

    def get_column_levels(self, covar_name):
        covar = self.__get_column(covar_name)
        return covar.get("levels", [])

    def check_usable(self):
        sample_id_col = [x for x in self.meta.get("columns", []) if x.get("class", "")=="sample_id"]
        if len(sample_id_col) != 1:
            return False, "Unable to find sample ID column"
        return True, ""

    def get_kinship_path(self, geno_id, must_exist=True):
        kinship = self.meta.get("kinship", None)
        if kinship:
            kinship_path = kinship.get(geno_id, None)
            if kinship_path:
                kinship_path = self.relative_path(kinship_path)
                if not must_exist or os.path.exists(kinship_path):
                    return kinship_path
        return None

    def as_object(self):
        obj = {key: getattr(self, key) for key in self.__dbfields if hasattr(self, key)} 
        obj["pheno_id"] = self.pheno_id
        obj["meta"] = self.meta
        obj["is_usable"], obj["usable_result"] = self.check_usable()
        return obj

    def __get_column(self, covar_name):
        covar = [x for x in self.meta.get("columns", []) if x.get("name", "")==covar_name]
        if len(covar) != 1:
            raise Exception("Could not find column: {}".format(covar_name))
        return covar[0]

    @staticmethod
    def get(pheno_id, config):
        where = WhereExpression("phenotypes.id = uuid_to_bin(%s)", (pheno_id,))
        return Phenotype.__get_by_sql_where(where=where, config=config)

    @staticmethod
    def get_by_hash_user(filehash, user_id, config):
        where = WhereAll(
            WhereExpression("md5sum=%s", (filehash,)),
            WhereExpression("user_id=%s", (user_id,)),
            WhereExpression("phenotypes.is_active=1")
        )
        return Phenotype.__get_by_sql_where(where=where, config=config)
    
    @staticmethod
    def __get_by_sql_where(where, config):
        db = sql_pool.get_conn()
        results = Phenotype.__list_by_sql_where_query(db, where=where).results
        if len(results) != 1:
            return None
        result = results[0]
        pheno_id = result["id"]
        pheno_folder = os.path.join(config.get("PHENO_DATA_FOLDER", "./"), pheno_id)
        meta_path = os.path.expanduser(os.path.join(pheno_folder, "meta.json"))
        if os.path.exists(meta_path):
            with open(meta_path) as meta_file:
                meta = json.load(meta_file)
        else:
           meta = dict()
        p = Phenotype(pheno_id, meta)
        p.root_path = pheno_folder
        for x in Phenotype.__dbfields:
            if x in result:
                setattr(p, x, result[x])
        return p

    @staticmethod
    def list_all_for_user(user_id, config=None, query=None):
        db = sql_pool.get_conn()
        where = WhereAll(
            WhereExpression("phenotypes.is_active = 1"),
            WhereExpression("phenotypes.user_id = %s", (user_id,))
        )
        result = Phenotype.__list_by_sql_where_query(db, where=where, query=query)
        return result

    @staticmethod
    def list_all(config=None, query=None):
        db = sql_pool.get_conn()
        result = Phenotype.__list_by_sql_where_query(db, query=query)
        return result

    @staticmethod
    def __list_by_sql_where_query(db, where=None, query=None):
        cols = OrderedDict([("id", "bin_to_uuid(phenotypes.id)"),
            ("name", "phenotypes.name"),
            ("description", "phenotypes.description"),
            ("user_email", "users.email"),
            ("user_id", "phenotypes.user_id"),
            ("orig_file_name", "phenotypes.orig_file_name"),
            ("md5sum", "phenotypes.md5sum"),
            ("creation_date", "DATE_FORMAT(phenotypes.creation_date, '%%Y-%%m-%%d %%H:%%i:%%s')"),
            ("is_active", "phenotypes.is_active")])
        qcols = ["id", "name", "user_email"]
        page, order_by, qsearch = SelectQuery.translate_query(query, cols, qcols)
        if not order_by:
            order_by = OrderClause(OrderExpression(cols["creation_date"], "DESC"))
        sqlcmd = (SelectQuery()
            .set_cols([ "{} AS {}".format(v,k) for k,v in cols.items()])
            .set_table("phenotypes")
            .add_join(TableJoin("users", "phenotypes.user_id = users.id"))
            .set_where(where)
            .set_search(qsearch)
            .set_order_by(order_by)
            .set_page(page))
        return PagedResult.execute_select(db, sqlcmd)

    @staticmethod
    def add(values):
        if "id" in values:
            pheno_id = values["id"]
            del values["id"]
        else:
            raise Exception("Missing required field: id")
        updateable_fields = ["name", "user_id", "orig_file_name", "md5sum"]
        fields = list(values.keys()) 
        values = [None if x=="" else x for x in new_values.values()]
        bad_fields = [x for x in fields if x not in updateable_fields]
        if len(bad_fields)>0:
            raise Exception("Invalid field: {}".format(", ".join(bad_fields)))
        sql = "INSERT INTO phenotypes (id, {}) VALUES (uuid_to_bin(%s), {})".format( \
            ", ".join(fields),  \
            ", ".join(["%s"] * len(fields)) )
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute(sql, [pheno_id] + values)
        db.commit()

    @staticmethod
    def update(pheno_id, new_values):
        updateable_fields = ["name", "description"]
        fields = list(new_values.keys()) 
        values = [None if x=="" else x for x in new_values.values()]
        bad_fields = [x for x in fields if x not in updateable_fields]
        if len(bad_fields)>0:
            raise Exception("Invalid update field: {}".format(", ".join(bad_fields)))
        sql = "UPDATE phenotypes SET "+ \
            ", ".join(("{}=%s".format(k) for k in fields)) + \
            " WHERE id = uuid_to_bin(%s)"
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute(sql, values + [pheno_id])
        db.commit()

    @staticmethod
    def retire(pheno_id, config=None):
        pheno = Phenotype.get(pheno_id, config)
        if pheno:
            db = sql_pool.get_conn()
            cur = db.cursor()
            sql = "UPDATE phenotypes SET is_active=0 WHERE id = uuid_to_bin(%s)"
            cur.execute(sql, (pheno_id, ))
            affected = cur.rowcount
            db.commit()
        else:
            raise Exception("Pheno {} Not Found".format(pheno_id))

    @staticmethod
    def purge(pheno_id, config=None):
        pheno = Phenotype.get(pheno_id, config)
        if pheno:
            db = sql_pool.get_conn()
            cur = db.cursor()
            sql = "DELETE FROM phenotypes WHERE id = uuid_to_bin(%s)"
            cur.execute(sql, (pheno_id, ))
            affected = cur.rowcount
            db.commit()

            removed = False
            pheno_directory = pheno.root_path 
            if os.path.isdir(pheno_directory) and affected>0:
                try:
                    shutil.rmtree(pheno_directory)
                    removed = True
                except:
                    pass

            result = {"records": affected, "files": removed}
            return result
        else:
            raise Exception("Pheno {} Not Found".format(pheno_id))
