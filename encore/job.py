import os
import shutil
import json
from . import sql_pool
import MySQLdb
import sys
import re
import hashlib
from collections import OrderedDict
from .user import User
from .model_factory import ModelFactory
from .db_helpers import SelectQuery, TableJoin, PagedResult, OrderClause, OrderExpression, WhereExpression, WhereAll

class Job:
    __dbfields = ["user_id", "name", "description", "error_message", "status_id",
        "creation_date", "modified_date", "is_active"]
    __extfields = ["status", "role"]

    def __init__(self, job_id, meta=None, config=None):
        self.job_id = job_id
        for x in self.__dbfields + self.__extfields:
            setattr(self, x, None)
        self.root_path = "" 
        self.users = []
        self.meta = meta
        self.config = config

    def get_adjusted_phenotypes(self):
        phe_file = self.relative_path("output.phe")
        phenos = {}
        if os.path.exists(phe_file):
            with open(phe_file) as f:
                for line in f:
                    (sample, val) = line.split()
                    phenos[sample] = float(val)
        else:
            model = self.get_model(self.config)
            model_spec = self.meta
            geno = model.get_geno(model_spec)

            pheno = model.get_pheno(model_spec)
            phenos = model.get_response_values(model_spec, geno, pheno)
        return phenos

    def relative_path(self, *args):
        return os.path.expanduser(os.path.join(self.root_path, *args))

    def get_genotype_id(self):
        return self.meta.get("genotype", None) 

    def get_phenotype_id(self):
        return self.meta.get("phenotype", None)

    def get_model(self, config=None):
        return ModelFactory.get(self.meta.get("type", None), self.root_path, config)

    def get_output_files(self):
        files = []
        def add_if_exists(rel_path, display_name, primary=False):
            file_path = self.relative_path(rel_path)
            if os.path.exists(file_path):
                files.append({"path": rel_path, "size": os.path.getsize(file_path), 
                    "name": display_name, "primary": primary})
        add_if_exists("output.epacts.gz", "Epacts Results", True)
        add_if_exists("results.txt.gz", "SAIGE Results", True)
        add_if_exists("output.filtered.001.gz", "Filtered Results (p-val<0.001)")
        return files

    def get_output_primary_file(self):
        files = [x for x in self.get_output_files() if x.get("primary", False)]
        if len(files)==1:
            return files[0]["path"]
        else:
            return None

    def get_output_file_path(self):
        file_name = self.get_output_primary_file()
        if file_name:
            return self.relative_path(file_name)
        else:
            return None

    def get_owner(self):
        return User.from_id(self.user_id) 

    def get_param_hash(self):
        return Job.calc_param_hash(self.meta)

    def get_failure_reason(self, config = None):
        model = self.get_model(config)
        if model:
            return model.get_failure_reason()
        return None

    def as_object(self):
        obj = {key: getattr(self, key) for key in self.__dbfields  + self.__extfields if hasattr(self, key)} 
        obj["job_id"] = self.job_id
        obj["users"] = self.users
        obj["output_files"] = self.get_output_files()
        if self.meta:
            details = self.meta.copy()
            model = self.get_model()
            if model:
                details["model_desc"] = model.model_desc
                if "variant_filter" in details:
                    details["variant_filter_desc"] = model.get_filter_desc(details["variant_filter"])
            obj["details"] = details
            obj["hash"] = self.get_param_hash()
        return obj

    @staticmethod
    def get(job_id, config):
        if not re.match('[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}', job_id):
            return None
        job_folder = os.path.join(config.get("JOB_DATA_FOLDER", "./"), job_id)
        meta_path = os.path.expanduser(os.path.join(job_folder, "job.json"))
        if os.path.exists(meta_path):
            with open(meta_path) as meta_file:
                meta = json.load(meta_file)
        else:
           meta = dict()
        j = Job(job_id, meta, config=config)
        j.root_path = job_folder
        db = sql_pool.get_conn()
        cur = db.cursor(MySQLdb.cursors.DictCursor)
        sql = """
            SELECT
              bin_to_uuid(jobs.id) AS id,
              jobs.name AS name, jobs.description as description, jobs.user_id as user_id,
              jobs.status_id as status_id, statuses.name AS status,
              jobs.error_message AS error_message,
              DATE_FORMAT(jobs.creation_date, '%%Y-%%m-%%d %%H:%%i:%%s') AS creation_date,
              DATE_FORMAT(jobs.modified_date, '%%Y-%%m-%%d %%H:%%i:%%s') AS modified_date,
              jobs.is_active
            FROM jobs
            LEFT JOIN statuses ON jobs.status_id = statuses.id
            WHERE jobs.id = uuid_to_bin(%s)
            """
        cur.execute(sql, (job_id,))
        result = cur.fetchone()
        if result is not None:
            for x in Job.__dbfields + Job.__extfields:
                if x in result:
                    setattr(j, x, result[x])
            sql = """
                SELECT 
                    ju.user_id as user_id, ju.role_id as role_id,
                    role.role_name as role, users.email, 
                    DATE_FORMAT(ju.modified_date, '%%Y-%%m-%%d %%H:%%i:%%s') AS modified_date
                FROM job_users as ju
                    LEFT JOIN job_user_roles role on role.id = ju.role_id
                    LEFT JOIN users on users.id = ju.user_id
                WHERE 
                    ju.job_id = uuid_to_bin(%s)
                """
            cur.execute(sql, (job_id,))
            result = cur.fetchall()
            j.users = result
            return j
        else:
            return None

    @staticmethod
    def list_all_for_user(user_id, config=None, query=None):
        db = sql_pool.get_conn()
        params = query.params
        params["is_active"] = True
        params["user_id"] = user_id
        where, joins = Job.__params_to_where(params)
        results = Job.__list_by_sql_where_query(db, where=where, query=query)
        return results


    @staticmethod
    def list_all_for_user_shared_with(user_id, shared_user_id, config=None, query=None):
        db = sql_pool.get_conn()
        params = query.params
        params["is_active"] = True
        params["user_id"] = user_id
        params["shared_with"] = shared_user_id
        where, joins = Job.__params_to_where(params)
        results = Job.__list_by_sql_where_query(db, where=where, query=query)
        return results

    @staticmethod
    def list_all_for_phenotype(pheno_id, config=None, query=None):
        db = sql_pool.get_conn()
        params = query.params
        params["is_active"] = True
        params["pheno_id"] = pheno_id
        where, joins = Job.__params_to_where(params)
        results = Job.__list_by_sql_where_query(db, where=where, query=query)
        return results

    @staticmethod
    def list_all_for_genotype(geno_id, config=None, query=None):
        db = sql_pool.get_conn()
        params = query.params
        params["geno_id"] = geno_id
        where, joins = Job.__params_to_where(params)
        results = Job.__list_by_sql_where_query(db, where=where, query=query)
        return results

    @staticmethod
    def list_all_for_user_by_genotype(user_id, geno_id, config=None, query=None):
        db = sql_pool.get_conn()
        params = query.params
        params["is_active"] = True
        params["user_id"] = user_id
        params["geno_id"] = geno_id
        where, joins = Job.__params_to_where(params)
        results = Job.__list_by_sql_where_query(db, where=where, query=query)
        return results 

    @staticmethod
    def list_all_for_user_by_hash(user_id, param_hash, find_canceled=False, config=None):
        db = sql_pool.get_conn()
        where = ("jobs.param_hash = %s " +
            "AND jobs.id IN (SELECT job_id from job_users where user_id=%s) " +
            "AND jobs.is_active=1")
        if not find_canceled:
            where += " AND jobs.status_id not in (select id from statuses where name='canceled')"
        results = Job.__list_by_sql_where(db, where, (param_hash, user_id))
        return results 

    @staticmethod
    def list_all_active(config=None):
        db = sql_pool.get_conn()
        results = Job.__list_by_sql_where(db, ("("
            "statuses.name='queued' OR "
            "statuses.name='started' OR "
            "statuses.name='canceling')"))
        return results

    @staticmethod
    def list_all(config=None, query=None):
        db = sql_pool.get_conn()
        where, joins = Job.__params_to_where(query.params)
        result = Job.__list_by_sql_where_query(db, where=where, query=query)
        return result

    @staticmethod
    def __list_by_sql_where(db, where="", vals=()):
        where = WhereExpression(where, vals)
        result = Job.__list_by_sql_where_query(db, where=where, query=None)
        return result.results

    @staticmethod
    def __list_by_sql_where_query(db, where=None, query=None):
        cols = OrderedDict([("id", "bin_to_uuid(jobs.id)"),
            ("name", "jobs.name"),
            ("description", "jobs.description"),
            ("status", "statuses.name"),
            ("geno_id", "bin_to_uuid(jobs.geno_id)"),
            ("pheno_id", "bin_to_uuid(jobs.pheno_id)"),
            ("creation_date", "DATE_FORMAT(jobs.creation_date, '%%Y-%%m-%%d %%H:%%i:%%s')"),
            ("modified_date", "DATE_FORMAT(jobs.modified_date, '%%Y-%%m-%%d %%H:%%i:%%s')"),
            ("user_email", "users.email"),
            ("is_active", "jobs.is_active")])
        qcols = ["id", "name", "user_email", "status"]
        page, order_by, qsearch = SelectQuery.translate_query(query, cols, qcols)
        if not order_by:
            order_by = OrderClause(OrderExpression(cols["creation_date"], "DESC"))
        sqlcmd = (SelectQuery()
            .set_cols([ "{} AS {}".format(v,k) for k,v in cols.items()])
            .set_table("jobs")
            .add_join(TableJoin("statuses", "jobs.status_id = statuses.id"))
            .add_join(TableJoin("users", "jobs.user_id = users.id"))
            .set_where(where)
            .set_search(qsearch)
            .set_order_by(order_by)
            .set_page(page))
        return PagedResult.execute_select(db, sqlcmd)

    @staticmethod
    def __params_to_where(params):
        where = WhereAll()
        joins = dict()
        for k, v in params.items():
            if k == "user_id":
                where.add(WhereExpression("jobs.id IN (SELECT job_id from job_users where user_id=%s)", (v,)))
            elif k == "pheno_id":
                where.add(WhereExpression("jobs.pheno_id = uuid_to_bin(%s)", (v,)))
            elif k == "geno_id":
                where.add(WhereExpression("jobs.geno_id = uuid_to_bin(%s)", (v, )))
            elif k == "shared_with":
                where.add(WhereExpression("jobs.id IN (SELECT job_id from job_users where user_id=%s and role_id!=1)", (v,)))
            elif k == "is_active":
                if v:
                    where.add(WhereExpression("jobs.is_active=1"))
                else:
                    where.add(WhereExpression("jobs.is_active=0"))
            else:
                raise Exception("Parameter {} Not Recognized".format(k))
        return where, joins

    @staticmethod
    def create(job_id, values):
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute("""
            INSERT INTO jobs (id, name, description, user_id, geno_id, pheno_id, param_hash, status_id)
            VALUES (uuid_to_bin(%s), %s, %s, uuid_to_bin(%s), uuid_to_bin(%s), %s,
            (SELECT id FROM statuses WHERE name = 'queued'))
            """, (job_id, values["name"], values["description"], values["user_id"], values["genotype"],
            values["phenotype"], values["param_hash"]))
        cur.execute("""
            INSERT INTO job_users(job_id, user_id, created_by, role_id)
            VALUES (uuid_to_bin(%s), %s, %s, (SELECT id FROM job_user_roles WHERE role_name = 'owner'))
            """, (job_id, values["user_id"], values["user_id"]))
        db.commit()


    @staticmethod
    def update_status(job_id, new_status, error_message=None, old_status=None):
        update_fields = [
            "status_id = (SELECT id FROM statuses WHERE name=%s LIMIT 1)",
            "modified_date = NOW()"
        ]
        update_values = [new_status]
        if error_message is not None:
            if error_message != "":
                update_fields.append("error_message = %s")
                update_values.append(error_message)
            else:
                update_fields.append("error_message = NULL")
        where_fields = ["id = uuid_to_bin(%s)"]
        where_values = [job_id]
        if old_status is not None:
            where_fields.append("status_id = (SELECT id FROM statuses WHERE name=%s LIMIT 1)")
            where_values.append(old_status)
        sql = "UPDATE jobs"
        sql += " SET " + ", ".join(update_fields)
        sql += " WHERE " + " AND ".join(where_fields)
        values = update_values + where_values
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute(sql, values)
        db.commit()

    @staticmethod
    def update(job_id, new_values):
        updateable_fields = ["name", "description"]
        fields = list(new_values.keys()) 
        values = [None if x=="" else x for x in new_values.values()]
        bad_fields = [x for x in fields if x not in updateable_fields]
        if len(bad_fields)>0:
            raise Exception("Invalid update field: {}".format(", ".join(bad_fields)))
        sql = "UPDATE jobs SET "+ \
            ", ".join(("{}=%s".format(k) for k in fields)) + \
            " WHERE id = uuid_to_bin(%s)"
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute(sql, values + [job_id])
        db.commit()

    @staticmethod
    def cancel(job_id):
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute("""
            UPDATE jobs SET status_id = (SELECT id FROM statuses WHERE name = 'canceling')
            WHERE id = uuid_to_bin(%s)
            """, (job_id, ))
        db.commit()

    @staticmethod
    def resubmit(job_id):
        db = sql_pool.get_conn()
        cur = db.cursor()
        cur.execute("""
            UPDATE jobs SET status_id = (SELECT id FROM statuses WHERE name = 'queued'), error_message=""
            WHERE id = uuid_to_bin(%s)
            """, (job_id, ))
        db.commit()

    @staticmethod
    def retire(job_id, config=None):
        job = Job.get(job_id, config)
        if job:
            db = sql_pool.get_conn()
            cur = db.cursor()
            sql = "UPDATE jobs SET is_active=0 WHERE id = uuid_to_bin(%s)"
            cur.execute(sql, (job_id, ))
            affected = cur.rowcount
            db.commit()
        else:
            raise Exception("Job {} Not Found".format(job_id))

    @staticmethod
    def purge(job_id, config=None):
        job = Job.get(job_id, config)
        if job:
            db = sql_pool.get_conn()
            cur = db.cursor()
            sql = "DELETE FROM job_users WHERE job_id = uuid_to_bin(%s)"
            cur.execute(sql, (job_id, ))
            users = cur.rowcount
            sql = "DELETE FROM jobs WHERE id = uuid_to_bin(%s)"
            cur.execute(sql, (job_id, ))
            affected = cur.rowcount
            db.commit()

            removed = False
            job_directory = job.root_path 
            if os.path.isdir(job_directory) and affected>0:
                try:
                    shutil.rmtree(job_directory)
                    removed = True
                except:
                    pass

            result = {"records": affected, "users": users, "files": removed}
            return result
        else:
            raise Exception("Job {} Not Found".format(job_id))

    @staticmethod
    def share_add_email(job_id, email, current_user, role=0, config=None):
        ex = None
        db = sql_pool.get_conn()
        user = User.from_email(email, db)
        if user is None:
            user = User.create({"email": email, "can_analyze": False}, db)["user"]
        try:
            cur = db.cursor()
            sql = """
                INSERT INTO job_users (job_id, user_id, role_id, created_by)
                VALUES (uuid_to_bin(%s), %s, %s, %s)
                """
            cur.execute(sql, (job_id, user.rid, role, current_user.rid))
            db.commit()
        except:
            ex = sys.exc_info()[1]
        finally:
            cur.close()
        if ex is not None:
            raise ex
        return True

    @staticmethod
    def share_drop_email(job_id, email, current_user, role=0, config=None):
        ex = None
        db = sql_pool.get_conn()
        user = User.from_email(email, db)
        if user is None:
            return False
        try:
            cur = db.cursor()
            sql = """
                DELETE FROM job_users
                WHERE job_id=uuid_to_bin(%s) AND user_id=%s
                """
            cur.execute(sql, (job_id, user.rid))
            db.commit()
        except:
            ex = sys.exc_info()[1]
        finally:
            cur.close()
        if ex is not None:
            raise ex
        return True

    @staticmethod
    def share_drop_collaborator(owner_id, collaborator_id, config=None):
        ex = None
        db = sql_pool.get_conn()
        owner = User.from_id(owner_id, db)
        collaborator = User.from_id(collaborator_id, db)
        if owner is None:
            raise Exception("Unrecognized owner id: {}".format(owner_id))
        if collaborator is None:
            raise Exception("Unrecognized collaborator id: {}".format(collaborator_id))
        records = 0
        try:
            cur = db.cursor()
            sql = """
                DELETE FROM job_users
                WHERE user_id=%s and job_id in
                    (SELECT * FROM (SELECT job_id from job_users where user_id=%s and role_id=1) as hack)
                """
            cur.execute(sql, (collaborator.rid, owner.rid))
            records = cur.rowcount
            db.commit()
        except:
            ex = sys.exc_info()[1]
        finally:
            cur.close()
        if ex is not None:
            raise ex
        result = {"records": records}
        return result

    @staticmethod
    def counts(by=None, filters=None, config=None):
        join_users = False
        join_geno = False
        join_status = False
        if not by:
            by = []
        elif isinstance(by, str):
            by = by.split(",")
        if not filters:
            filters = []
        elif isinstance(filters, str):
            filters = filters.split(",")
        select = []
        group_by = []
        columns = []
        wheres = []
        for field in by:
            if field=="month":
                select += [ "DATE_FORMAT(jobs.creation_date, '%Y-%m') as month"]
                group_by += [ "DATE_FORMAT(jobs.creation_date, '%Y-%m')"]
                columns += ["month"]
            elif field == "year":
                select += ["year(jobs.creation_date) as year"]
                group_by += ["year(jobs.creation_date)"]
                columns += ["year"]
            elif field == "user":
                select += ["COALESCE(users.full_name, users.email) as user"]
                group_by += ["users.id"]
                columns += ["user"]
                join_users = True
            elif field == "status":
                select += ["statuses.name as status"]
                group_by += ["statuses.name"]
                columns += ["status"]
                join_status = True
            elif field == "geno":
                select += ["genotypes.name as geno"]
                group_by += ["genotypes.name"]
                columns += ["geno"]
                join_geno = True
            else:
                raise Exception("Unrecognized field: {}".format(field))
        for filt in filters:
            if filt == "successful":
                wheres += ["jobs.status_id = (select id from statuses where name = 'succeeded')"]
            else:
                raise Exception("Unrecognized filter: {}".format(filt))
        select += ["COUNT(*) as count"]
        columns += ["count"]
        sql = "SELECT " + ", ".join(select)
        sql += " FROM jobs"
        if join_users:
            sql += " JOIN users on jobs.user_id = users.id"
        if join_status:
            sql += " JOIN statuses on jobs.status_id = statuses.id"
        if join_geno:
            sql += " JOIN genotypes on jobs.geno_id = genotypes.id"
        if len(wheres):
            sql += " WHERE (" + "), (".join(wheres) + ")"
        if len(group_by):
            sql += " GROUP BY " + ", ".join(group_by)


        db = sql_pool.get_conn()
        cur = db.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(sql)
        results = cur.fetchall()
        return {"header": {"columns": columns}, "data": results}

    @staticmethod
    def calc_param_hash(meta):
        meta_clean = meta.copy();
        meta_clean.pop("name", None)
        meta_clean.pop("user_id", None)
        meta_clean.pop("response_desc", None)
        job_def_string = json.dumps(meta_clean, sort_keys=True)
        return hashlib.md5(job_def_string.encode('utf-8')).hexdigest()
