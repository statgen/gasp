from flask import Blueprint, Response, json, render_template, current_app, request, send_file, url_for
from flask_login import current_user, login_required
from werkzeug.urls import url_encode
from .user import User
from .job import Job 
from .auth import check_view_job, check_edit_job, can_user_edit_job, check_edit_pheno, admin_required, check_view_pheno
from .genotype import Genotype
from .phenotype import Phenotype
from .notice import Notice
from .pheno_reader import PhenoReader
from .slurm_queue import SlurmJob, get_queue
from .model_factory import ModelFactory
from .notifier import get_notifier
from .access_tracker import AccessTracker
from .db_helpers import PagedResult, PageInfo, QueryInfo
import os
import re
import gzip
import uuid
import tabix
import hashlib
import shutil
import collections
import sys, traceback
import subprocess
import requests
import numpy as np

api = Blueprint("api", __name__)

def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default

def get_page_info(params, default_limit = 0):
    offset = safe_cast(params.pop("offset", 0), int, 0)
    if "limit" in params:
        limit = safe_cast(params.pop("limit", 0), int, 0)
    else:
        limit = default_limit
    if limit==0 and offset==0:
        return None
    return PageInfo(limit, offset)

def get_order_info(params):
    if not "order_by" in params:
        return None
    order_by = []
    raw_vals = params.pop("order_by").split(",")
    for val in raw_vals:
        if val.startswith("+"):
            order_by.append((val[1:], "ASC"))
        elif val.startswith("-"):
            order_by.append((val[1:], "DESC"))
        else:
            order_by.append((val, "ASC"))
    return order_by

def get_search_info(params):
    return params.pop("q", None)

def get_query_info(request, default_limit = 200):
    params = request.args.to_dict(flat=True)
    page = get_page_info(params, default_limit)
    order_by = get_order_info(params)
    search = get_search_info(params)
    params.pop("echo", None)
    params.pop("_", None)
    return QueryInfo(page, order_by, search, params)

@api.before_request
@login_required
def before_request():
    # Just here to trigger the login_required before any request
    pass

@api.route("/", methods=["GET"])
def list_endpoints():
    endpoints = [
        {"key": "geno-list", "url": url_for("api.get_genotypes"),
            "description": "List available genotypes", "verb": "GET"},
        {"key": "job-list", "url": url_for("api.get_jobs"),
            "description": "List available jobs", "verb": "GET"},
        {"key": "job-get", "url": url_for("api.get_genotype", geno_id="_JOBID_"),
            "description": "Get specific job", "verb": "GET",
            "url-params": [{"key": "JOBID", "description": "Requested job ID", "pattern": "_JOBID_"}]},
        {"key": "pheno-list", "url": url_for("api.get_phenotypes"),
            "description": "List available phenotype files", "verb": "GET"},
        {"key": "pheno-get", "url": url_for("api.get_pheno", pheno_id="_PHENOID_"),
            "description": "Get specific phenotype", "verb": "GET",
            "url-params": [{"key": "PHENOID", "description": "Requested phenotype ID", "pattern": "_PHENOID_"}]},
    ]
    header = {"api_version": "0.1"}
    return ApiResult(endpoints, header=header)

@api.route("/genos", methods=["GET"])
def get_genotypes():
    query = get_query_info(request)
    genos = Genotype.list_all_for_user(current_user.rid, query=query)
    def get_stats(x):
        s = Genotype.get(x["id"],current_app.config).get_stats() 
        s["name"] = x["name"]
        s["creation_date"] = x["creation_date"]
        s["build"] = x["build"]
        s["id"] = x["id"]
        return s
    stats = [get_stats(x) for x in genos]
    genos.results = stats
    return ApiResult(genos, request=request)

@api.route("/genos/<geno_id>", methods=["GET"])
def get_genotype(geno_id):
    g = Genotype.get(geno_id, current_app.config)
    if g is None:
        raise ApiException("GENOTYPE NOT FOUND", 404)
    return ApiResult(g.as_object())

@api.route("/genos/<geno_id>/info", methods=["GET"])
def get_genotype_info_stats(geno_id):
    g = Genotype.get(geno_id, current_app.config)
    return ApiResult(g.get_info_stats())

@api.route("/genos/<geno_id>/chroms", methods=["GET"])
def get_genotype_chromosome_ranges(geno_id):
    g = Genotype.get(geno_id, current_app.config)
    return ApiResult(g.get_chromosome_ranges() or [])

@api.route("/genos/<geno_id>/jobs", methods=["GET"])
def get_genotype_jobs(geno_id):
    query = get_query_info(request)
    jobs = Job.list_all_for_user_by_genotype(current_user.rid, geno_id,
        config=current_app.config, query=query)
    return ApiResult(jobs, request=request)

@api.route("/jobs", methods=["POST"])
def create_new_job():
    user = current_user
    if not user.can_analyze:
        raise ApiException("USER ACTION NOT ALLOWED", 403)
    job_desc = dict()
    if request.method != 'POST':
        raise ApiException("NOT A POST REQUEST", 405)
    form_data = request.form
    genotype_id = form_data["genotype"]
    phenotype_id = form_data["phenotype"]
    job_desc["genotype"] = genotype_id
    job_desc["phenotype"] = phenotype_id
    job_desc["name"] = form_data["job_name"]
    job_desc["description"] = form_data.get("description", default=None)
    job_desc["response"] =  form_data["response"] 
    if form_data.get("response_invnorm", False):
        job_desc["response_invnorm"] = True
    job_desc["covariates"] =  form_data.getlist("covariates")
    job_desc["genopheno"] =  form_data.getlist("genopheno")
    job_desc["type"] = form_data["model"]
    if form_data.get("variant_filter", "").strip() != "":
        job_desc["variant_filter"] = form_data["variant_filter"]
    if form_data.get("region", "gwas").strip() != "gwas":
        job_desc["region"] = form_data["region"]
    job_desc["user_id"] = current_user.rid
    job_id = str(uuid.uuid4())

    if not job_id:
        raise ApiException("COULD NOT GENERATE JOB ID")
    job_directory =  os.path.join(current_app.config.get("JOB_DATA_FOLDER", "./"), job_id)

    job = SlurmJob(job_id, job_directory, current_app.config) 
    model = job.get_model(job_desc)

    try:
        model.validate_model_spec(job_desc)
    except Exception as e:
        print(e)
        raise ApiException("INVALID MODEL REQUEST", details=str(e))
    # valid model request
    try:
        param_hash = Job.calc_param_hash(job_desc)
        possible_dups = Job.list_all_for_user_by_hash(current_user.rid, param_hash)
        if len(possible_dups)>0:
            #already ran
            dup_jobs = []
            for dup_job in possible_dups:
                dup_job_id = dup_job["id"]
                dup_jobs.append({"id": dup_job_id,
                    "job_name": dup_job["name"],
                    "creation_date": dup_job["creation_date"],
                    "url_job": url_for("user.get_job", job_id=dup_job_id)
                })
            return ApiResult({"duplicates": dup_jobs}, 303)
    except Exception as e:
        print(e)
        raise ApiException("ERROR CHECKING FOR DUPLICATE REQUEST")
    # is not a duplicate
    try:
        os.mkdir(job_directory)
        job_desc_file = os.path.join(job_directory, "job.json")
        with open(job_desc_file, "w") as outfile:
            json.dump(job_desc, outfile)
    except Exception:
        raise ApiException("COULD NOT SAVE JOB DESCRIPTION")
    # file has been saved to disc
    try:
        job.submit_job(job_desc)
    except Exception as e:
        print(e)
        traceback.print_exc(file=sys.stdout)
        shutil.rmtree(job_directory)
        raise ApiException("COULD NOT ADD JOB TO QUEUE")
    # job submitted to queue
    try:
        job_desc["param_hash"] = param_hash
        Job.create(job_id, job_desc)
    except:
        shutil.rmtree(job_directory)
        raise ApiException("COULD NOT SAVE TO DATABASE")
    # everything worked
    return ApiResult({"id": job_id, "url_job": url_for("user.get_job", job_id=job_id)})

@api.route("/jobs", methods=["GET"])
def get_jobs():
    query = get_query_info(request)
    jobs = Job.list_all_for_user(current_user.rid, current_app.config, query=query)
    return ApiResult(jobs, request=request)

@api.route("/jobs/<job_id>", methods=["GET"])
@check_view_job
def get_job(job_id, job=None):
    return ApiResult(job.as_object())

@api.route("/jobs/<job_id>", methods=["DELETE"])
@check_edit_job
def retire_job(job_id, job=None):
    try:
        Job.retire(job_id, current_app.config)
        return ApiResult({"retired": True})
    except Exception as e:
        raise ApiException("COULD NOT RETIRE JOB", details=str(e))

@api.route("/jobs/<job_id>", methods=["POST"])
@check_edit_job
def update_job(job_id, job=None):
    try:
        values = request.values.to_dict(flat=True)
        Job.update(job_id, values)
        return ApiResult({"updated": True})
    except Exception as e:
        raise ApiException("COULD NOT UPDATE JOB", details=str(e))

@api.route("/jobs/<job_id>/share", methods=["POST"])
@check_edit_job
def share_job(job_id, job=None):
    form_data = request.form
    add = form_data["add"].split(",") 
    drop = form_data["drop"].split(",") 
    for address in (x for x in add if len(x)>0):
        Job.share_add_email(job_id, address, current_user)
    for address in (x for x in drop if len(x)>0):
        Job.share_drop_email(job_id, address, current_user)
    return ApiResult({"id": job_id, "url_job": url_for("user.get_job", job_id=job_id)})

@api.route("/jobs/<job_id>/resubmit", methods=["POST"])
@check_edit_job
def resubmit_job(job_id, job=None):
    sjob = SlurmJob(job_id, job.root_path, current_app.config) 
    rebuild = "rebuild" in request.args
    try:
        if rebuild:
            job_desc = sjob.load_model_spec()
            sjob.submit_job(job_desc)
        else:
            sjob.resubmit_job()
    except Exception as e:
        raise ApiException("Could not resubmit job", details=str(e));
    try:
        Job.resubmit(job_id)
    except:
        raise ApiException("Could not update job status");
    return ApiResult({"id": job_id, "url_job": url_for("user.get_job", job_id=job_id)})

@api.route("/jobs/<job_id>/cancel_request", methods=["POST"])
@check_edit_job
def cancel_job(job_id, job=None):
    if job is None:
        raise ApiException("JOB NOT FOUND", 404)
    slurmjob = SlurmJob(job_id, job.root_path, current_app.config) 
    try:
        slurmjob.cancel_job()
    except Exception as exception:
        print(exception)
        raise ApiException("COULD NOT CANCEL JOB")
    try:
        Job.cancel(job_id)
    except Exception as exception:
        print(exception)
        raise ApiException("COULD NOT UPDATE DB")
    return ApiResult({"message": "Job canceled"})

@api.route("/jobs/<job_id>/results", methods=["get"])
@check_view_job
def get_job_results(job_id, job=None):
    filters = request.args.to_dict()
    epacts_filename = job.relative_path("output.epacts.gz")
    with gzip.open(epacts_filename, "rt") as f:
        header = f.readline().rstrip('\n').split('\t')
        if header[1] == "BEG":
            header[1] = "BEGIN"
        if header[0] == "#CHROM":
            header[0] = "CHROM"
    assert len(header) > 0
    headerpos = {x:i for i,x in enumerate(header)}

    if filters.get("region", ""):
        tb = tabix.open(epacts_filename)
        indata = tb.query(chrom, start_pos, end_pos)
    else:
        indata = (x.split("\t") for x in gzip.open(epacts_filename))

    pass_tests = []
    if filters.get("non-monomorphic", False):
        if "AC" not in headerpos:
            raise Exception("Column AC not found")
        ac_index = headerpos["AC"]
        def mono_pass(row):
            if float(row[ac_index])>0:
                return True
            else:
                return False
        pass_tests.append(mono_pass)

    if "max-pvalue" in filters:
        if "PVALUE" not in headerpos:
            raise Exception("Column PVALUE not found")
        pval_index = headerpos["PVALUE"]
        thresh = float(filters.get("max-pvalue", 1))
        def pval_pass(row):
            if row[pval_index] == "NA":
                return False
            if float(row[pval_index])<thresh:
                return True
            else:
                return False
        pass_tests.append(pval_pass)

    def pass_row(row):
        if len(pass_tests)==0:
            return True
        for f in pass_tests:
            if not f(row):
                return False
        return True

    def generate():
        yield "\t".join(header) + "\n"
        next(indata) #skip header
        for row in indata:
            if pass_row(row):
                yield "\t".join(row)

    return Response(generate(), mimetype="text/plain")

def get_job_output(job, filename, as_attach=False, mimetype=None, tail=None, head=None):
    try:
        output_file = job.relative_path(filename)
        if tail or head:
            if tail and head:
                return "Cannot specify tail AND head", 500
            cmd = "head" if head else "tail"
            count = tail or head
            p = subprocess.Popen([cmd, "-n", count, output_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            tail_data, tail_error = p.communicate()
            resp = make_response(tail_data.decode())
            if as_attach:
                resp.headers["Content-Disposition"] = "attachment; filename={}".format(filename)
            if mimetype:
                resp.headers["Content-Type"] = mimetype
            return resp
        else:
            return send_file(output_file, as_attachment=as_attach, mimetype=mimetype)
    except Exception as e:
        print(e)
        return "File Not Found", 404

@api.route("/jobs/<job_id>/tables/top", methods=["GET"])
@check_view_job
def get_job_tophits(job_id, job=None):
    return get_job_output(job, "tophits.json", False)

@api.route("/jobs/<job_id>/plots/qq", methods=["GET"])
@check_view_job
def get_api_job_qq(job_id, job=None):
    return get_job_output(job, "qq.json")

@api.route("/jobs/<job_id>/plots/manhattan", methods=["GET"])
@check_view_job
def get_api_job_manhattan(job_id, job=None):
    return get_job_output(job, "manhattan.json")

@api.route("/jobs/<job_id>/plots/zoom", methods=["GET"])
@check_view_job
def get_job_zoom(job_id, job=None):
    header = []
    output_filename = job.get_output_file_path()
    with gzip.open(output_filename, "rt") as f:
        header = f.readline().rstrip('\n').split('\t')
        if header[1] == "BEG":
            header[1] = "BEGIN"
        if header[1] == "POS":
            header[1] = "BEGIN"
        if header[0] == "#CHROM":
            header[0] = "CHROM"
        if header[0] == "CHR":
            header[0] = "CHROM"
        if len(header)>6 and header[6] == "AF_Allele2":
            header[6] = "MAF"
        if len(header)>7 and header[7] == "N":
            header[7] = "NS"
        if len(header)>11 and header[11] == "p.value":
            header[11] = "PVALUE"
        if len(header)>12 and header[12] == "p.value":
            header[12] = "PVALUE"
    assert len(header) > 0
    chrom = request.args.get("chrom", "")
    start_pos = int(request.args.get("start_pos", "0"))
    end_pos = int(request.args.get("end_pos", "0"))

    if chrom == "":
        return ApiResult(None, header={"variant_columns": header})

    headerpos = {x:i for i,x in enumerate(header)}
    tb = tabix.open(output_filename)
    try:
        results = tb.query(chrom, start_pos, end_pos)
    except:
        if chrom.startswith("chr"):
            chrom = chrom.replace("chr","")
        else:
            chrom = "chr" + chrom
        results = tb.query(chrom, start_pos, end_pos)

    json_response_data = dict()

    json_response_data["CHROM"] = []
    json_response_data["BEGIN"] = []
    json_response_data["MARKER_ID"] = []
    json_response_data["PVALUE"] = []
    if "END" in headerpos:
        json_response_data["END"] = []
    if "NS" in headerpos:
        json_response_data["NS"] = []
    if "MAF" in headerpos:
        json_response_data["MAF"] = []
    if "BETA" in headerpos:
        json_response_data["BETA"] = []
    for r in results:
        if r[headerpos["PVALUE"]] != "NA":
            json_response_data["CHROM"].append(r[headerpos["CHROM"]])
            json_response_data["BEGIN"].append(int(r[headerpos["BEGIN"]]))
            if "END" in headerpos:
                json_response_data["END"].append(r[headerpos["END"]])
            if "MARKER_ID" in headerpos:
                json_response_data["MARKER_ID"].append(r[headerpos["MARKER_ID"]])
            else:
                var1 = "{}:{}".format(r[headerpos["CHROM"]], r[headerpos["BEGIN"]])
                if "Allele1" in headerpos and "Allele2" in headerpos:
                    var1 = "{}_{}/{}".format(var1, r[headerpos["Allele1"]], r[headerpos["Allele2"]])
                json_response_data["MARKER_ID"].append(var1)
            json_response_data["PVALUE"].append(r[headerpos["PVALUE"]])
            if "NS" in headerpos:
                json_response_data["NS"].append(r[headerpos["NS"]])
            if "MAF" in headerpos:
                maf = float(r[headerpos["MAF"]])
                if maf > .5:
                    maf = 1-maf
                json_response_data["MAF"].append(str(maf))
            if "BETA" in headerpos:
                json_response_data["BETA"].append(r[headerpos["BETA"]])
    return ApiResult(json_response_data, header={"variant_columns": list(json_response_data.keys())}) 

def merge_info_stats(info, info_stats):
    info_extract = re.compile(r'([A-Z0-9_]+)(?:=([^;]+))?(?:;|$)')
    matches = info_extract.findall(info)
    merged = dict()
    if "fields" in info_stats:
        merged[".fieldorder"] = info_stats["fields"]
    for match in matches:
        field = match[0]
        value = match[1]
        if "desc" in info_stats and field in info_stats["desc"]:
            stats = dict(info_stats["desc"][field])
        else:
            stats = dict()
        stats["value"] = value
        merged[field] = stats
    return merged

@api.route("/jobs/<job_id>/plots/pheno", methods=["GET"])
@check_view_job
def get_job_variant_pheno(job_id, job=None):
    chrom = request.args.get("chrom", None)
    pos = request.args.get("pos", None)
    variant_id = request.args.get("variant_id", None)
    if (chrom is None or pos is None):
        raise ApiException("MISSING REQUIRED PARAMETER (chrom, pos)", 405)
    pos = int(pos)
    geno = Genotype.get(job.meta["genotype"], current_app.config)
    reader = geno.get_geno_reader(current_app.config)
    try:
        variant = reader.get_variant(chrom, pos, variant_id, annotate=True)
    except Exception as e:
        print(e)
        raise ApiException("Unable to retrieve genotypes", details=str(e))
    info_stats = geno.get_info_stats()
    info = variant["INFO"]
    calls = variant["GENOS"]
    del variant["GENOS"]
    variant["INFO"] = merge_info_stats(info, info_stats)
    phenos = job.get_adjusted_phenotypes()
    call_pheno = collections.defaultdict(list) 
    for sample, value in phenos.items():
        sample_geno = calls[sample]
        call_pheno[sample_geno].append(value)
    summary = {}
    for genotype, observations in call_pheno.items():
        obs_array = np.array(observations)
        q1 = np.percentile(obs_array, 25)
        q3 = np.percentile(obs_array, 75)
        iqr = (q3-q1)*1.5
        outliers = [x for x in obs_array if x<q1-iqr or x>q3+iqr]
        upper_whisker = obs_array[obs_array<=q3+iqr].max()
        lower_whisker = obs_array[obs_array>=q1-iqr].min()
        summary[genotype] = {
            "min": np.amin(obs_array),
            "w1": lower_whisker,
            "q1": q1,
            "mean": obs_array.mean(),
            "q2": np.percentile(obs_array, 50),
            "q3": q3,
            "w3": upper_whisker,
            "max": np.amax(obs_array),
            "n":  obs_array.size,
            "outliers": outliers
        }
    return ApiResult(summary, header=variant)

@api.route("/jobs/<job_id>/progress", methods=["GET"])
@check_view_job
def get_job_progress(job_id, job=None):
    sej = SlurmJob(job_id, job.root_path, current_app.config)
    return ApiResult(sej.get_progress())

@api.route("/queue", methods=["GET"])
@api.route("/queue/<job_id>", methods=["GET"])
def get_queue_status(job_id=None):
    queue = get_queue()
    summary = {"running": len(queue["running"]),
        "queued": len(queue["queued"])}
    if job_id is not None:
        try:
            position = [x["job_name"] for x in queue["queued"]].index(job_id) + 1
            summary["position"] = position
        except:
            pass
    return ApiResult(summary) 

@api.route("/phenos", methods=["GET"])
def get_phenotypes():
    query = get_query_info(request)
    phenos = Phenotype.list_all_for_user(current_user.rid, query=query)
    return ApiResult(phenos, request=request)

@api.route("/phenos/<pheno_id>", methods=["GET"])
@check_view_pheno
def get_pheno(pheno_id, pheno=None):
    return ApiResult(pheno.as_object())

@api.route("/phenos/<pheno_id>", methods=["POST"])
@check_edit_pheno
def update_pheno(pheno_id, pheno=None):
    try:
        values = request.values.to_dict(flat=True)
        Phenotype.update(pheno_id, values)
        return ApiResult({"updated": True})
    except Exception as e:
        raise ApiException("COULD NOT UPDATE PHENO", details=str(e))

@api.route("/phenos/<pheno_id>", methods=["DELETE"])
@check_edit_pheno
def retire_pheno(pheno_id, pheno=None):
    try:
        Phenotype.retire(pheno_id, current_app.config)
        return ApiResult({"retired": True})
    except Exception as e:
        raise ApiException("COULD NOT RETURE PHENO", details=str(e))

@api.route("/phenos/<pheno_id>/jobs", methods=["GET"])
def get_phenotype_jobs(pheno_id):
    query = get_query_info(request)
    jobs = Job.list_all_for_phenotype(pheno_id, config=current_app.config, query=query)
    return ApiResult(jobs, request=request)

def calculate_overlaps(pheno):
    genos = Genotype.list_all_for_user(current_user.rid)
    overlap_all = []
    for geno in genos:
        overlap = calculate_overlap(pheno, geno["id"])
        if overlap is not None:
            geno["overlap"] = overlap
            overlap_all.append(geno)
    return overlap_all

def calculate_overlap(pheno, geno_id):
    p_samples = pheno.get_pheno_reader().get_samples()
    g_samples = set(Genotype.get(geno_id, current_app.config).get_samples())
    if len(g_samples)<1:
        return None
    samples = 0
    matched = 0
    for sample in p_samples:
        samples += 1
        if sample in g_samples:
            matched += 1
    if samples < 1:
        return None
    return matched

@api.route("/phenos/<pheno_id>/overlap", methods=["GET"])
@check_edit_pheno
def get_pheno_sample_overlap_all(pheno_id, pheno=None):
    try:
        return ApiResult(calculate_overlaps(pheno))
    except Exception as e:
        raise ApiException("COULD NOT FIND OVERLAP", details=str(e))

@api.route("/phenos/<pheno_id>/overlap/<geno_id>", methods=["GET"])
@check_edit_pheno
def get_pheno_sample_overlap(pheno_id, geno_id, pheno=None):
    overlap = calculate_overlap(pheno, geno_id)
    if overlap is not None:
        return ApiResult({"samples": overlap})
    else:
        raise Exception("NO SAMPLES LIST STORED WITH GENOTYPES")

def suggest_pheno_name(filename):
    base, ext = os.path.splitext(os.path.basename(filename))
    base = base.replace("_", " ")
    return base

def hashfile(afile, hasher=None, blocksize=65536):
    if not hasher:
        hasher = hashlib.md5()
    buf = afile.read(blocksize)
    while len(buf) > 0:
        hasher.update(buf)
        buf = afile.read(blocksize)
    return hasher.digest()

@api.route("/phenos", methods=["POST"])
def post_pheno():
    user = current_user
    if not user.can_analyze:
        raise ApiException("User Action Not Allowed")
    if "pheno_file" not in request.files:
        raise ApiException("FILE NOT SENT")
    pheno_id = str(uuid.uuid4())
    if not pheno_id:
        raise ApiException("COULD NOT GENERATE PHENO ID")
    pheno_file = request.files["pheno_file"]
    orig_file_name = pheno_file.filename
    pheno_name = suggest_pheno_name(orig_file_name)
    pheno_directory = os.path.join(current_app.config.get("PHENO_DATA_FOLDER", "./"), pheno_id)
    try:
        os.mkdir(pheno_directory)
        pheno_file_path = os.path.join(pheno_directory, "pheno.txt")
        pheno_meta_path = os.path.join(pheno_directory, "meta.json")
        pheno_file.save(pheno_file_path)
        md5 =  hashfile(open(pheno_file_path, "rb")).hex()
    except Exception as e:
        print("File saving error: %s" % e)
        raise ApiException("COULD NOT SAVE FILE")
    # file has been saved to server
    existing_pheno = Phenotype.get_by_hash_user(md5, user.rid, current_app.config)
    if existing_pheno:
        shutil.rmtree(pheno_directory)
        pheno_id = existing_pheno.pheno_id
        pheno_dict = existing_pheno.as_object()
        pheno_dict["id"] = pheno_id
        pheno_dict["url_model"] = url_for("user.get_model_build", pheno=pheno_id)
        pheno_dict["url_view"] = url_for("user.get_pheno", pheno_id=pheno_id)
        pheno_dict["existing"] = True
        return ApiResult(pheno_dict)
    # file has not been uploaded before
    istext, filetype, mimetype = PhenoReader.is_text_file(pheno_file_path)
    if not istext:
        shutil.rmtree(pheno_directory)
        raise ApiException("NOT A RECOGNIZED TEXT FILE",
            details = {"filetype": filetype,
                "mimetype": mimetype})
    try:
        Phenotype.add({"id": pheno_id,
            "user_id": user.rid,
            "name": pheno_name,
            "orig_file_name": orig_file_name,
            "md5sum": md5})
    except Exception as e:
        print("Databse error: %s" % e)
        shutil.rmtree(pheno_directory)
        raise ApiException("COULD NOT SAVE TO DATABASE")
    # file has been saved to DB
    pheno = Phenotype.get(pheno_id, current_app.config)
    pheno_reader = pheno.get_pheno_reader()
    # find samples ids
    latest_geno = next(iter(Genotype.list_all_for_user(user)), None)
    if latest_geno:
        latest_geno = Genotype.get(latest_geno["id"], current_app.config)
        meta = pheno_reader.infer_meta( sample_ids = latest_geno.get_samples() )
    else:
        meta = pheno_reader.infer_meta()
    pheno.meta = meta
    line_count = sum(1 for _ in pheno_reader.row_extractor()) 
    meta["records"] = line_count
    with open(pheno_meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    result = {"id": pheno_id,  \
        "url_model": url_for("user.get_model_build", pheno=pheno_id), \
        "url_view": url_for("user.get_pheno", pheno_id=pheno_id)}
    # check that it's a "valid" phenotype
    is_usable, usable_error = pheno.check_usable()
    if not is_usable:
        result["error"] = usable_error 
        del result["url_model"]
    return ApiResult(result)

@api.route("/collaborations", methods=["GET"])
@login_required
def get_collaborations():
    query = get_query_info(request)
    return ApiResult(current_user.get_collaborations(query))

@api.route("/collaborations/people", methods=["GET"])
@login_required
def get_collaborators():
    query = get_query_info(request)
    users = current_user.get_collaborators(query)
    for user in users:
        user["url_view"] =  url_for("user.get_collaborations_with", user_id=user["id"])
    return ApiResult(users)

@api.route("/collaborations/people/<user_id>", methods=["GET"])
@login_required
def get_collaborations_with_user(user_id):
    query = get_query_info(request)
    jobs = Job.list_all_for_user_shared_with(current_user.rid, user_id, query=query)
    return ApiResult(jobs)

@api.route("/collaborations/people/<user_id>", methods=["DELETE"])
@login_required
def delete_collaborations_with_user(user_id):
    try:
        result = Job.share_drop_collaborator(current_user.rid, user_id)
        return ApiResult(result)
    except Exception as e:
        raise ApiException("COULD NOT DROP COLLABORATOR", details=str(e))

@api.route("/models", methods=["GET"])
@login_required
def get_models():
    models = ModelFactory.list(current_app.config)
    return ApiResult(models)

# ADMIN ENDPOINTS

@api.route("/jobs-all", methods=["GET"])
@admin_required
def get_jobs_all():
    query = get_query_info(request)
    jobs = Job.list_all(current_app.config, query=query)
    return ApiResult(jobs, request=request)

@api.route("/genos/<geno_id>/jobs-all", methods=["GET"])
@admin_required
def get_api_genotype_jobs_all(geno_id):
    query = get_query_info(request)
    jobs = Job.list_all_for_genotype(geno_id, current_app.config, query=query)
    return ApiResult(jobs, request=request)

@api.route("/users-all", methods=["GET"])
@admin_required
def get_users_all():
    query = get_query_info(request)
    users = User.list_all(current_app.config, query=query)
    return ApiResult(users, request=request)

@api.route("/phenos-all", methods=["GET"])
@admin_required
def get_api_phenos_all():
    query = get_query_info(request)
    phenos = Phenotype.list_all(current_app.config, query=query)
    return ApiResult(phenos, request=request)

@api.route("/genos-all", methods=["GET"])
@admin_required
def get_api_genos_all():
    query = get_query_info(request)
    genos = Genotype.list_all(current_app.config, query=query)
    if "links" in request.args:
        for geno in genos:
            geno["url_edit"] =  url_for("admin.get_admin_geno_detail_geno", geno_id=geno["id"])
    return ApiResult(genos, request=request)

@api.route("/users", methods=["POST"])
@admin_required
def add_user():
    try: 
        values = request.values.to_dict(flat=True)
        result = User.create(values)
        result["user"] = result["user"].as_object()
        result["created"] = True
        return ApiResult(result)
    except Exception as e:
        print(e)
        raise ApiException("COULD NOT ADD USER", details=str(e))

@api.route("/users/<user_id>", methods=["GET"])
@admin_required
def get_api_user(user_id):
    user = User.from_id(user_id).as_object()
    return ApiResult(user)

@api.route("/users/<user_id>", methods=["POST"])
@admin_required
def update_user(user_id):
    try:
        values = request.values.to_dict(flat=True)
        User.update(user_id, values)
        return ApiResult({"updated": True})
    except Exception as e:
        print(e)
        raise ApiException("COULD NOT UPDATE USER", details=str(e))

@api.route("/genos", methods=["POST"])
@admin_required
def add_geno():
    try:
        values = request.values.to_dict(flat=True)
        result = Genotype.create(values, config=current_app.config)
        result["geno"] = result["geno"].as_object()
        result["created"] = True
        return ApiResult(result)
    except Exception as e:
        print(e)
        raise ApiException("COULD NOT CREATE GENO", details=str(e))

@api.route("/jobs/<job_id>/purge", methods=["DELETE"])
@admin_required
def purge_job(job_id):
    try:
        result = Job.purge(job_id, current_app.config)
        result["purged"] = True
        return ApiResult(result)
    except Exception as e:
        raise ApiException("COULD NOT PURGE JOB", details=str(e))

@api.route("/jobs/counts", methods=["GET"])
@admin_required
def get_job_counts():
    try:
        by = request.args.get("by")
        filters = request.args.get("filter")
        results = Job.counts(by=by, filters=filters, config=current_app.config)
        return ApiResult(results)
    except Exception as e:
        print(e)
        raise ApiException("COULD COUNT JOBS", details=str(e))

@api.route("/users/counts", methods=["GET"])
@admin_required
def get_user_counts():
    try:
        by = request.args.get("by")
        filters = request.args.get("filter")
        results = User.counts(by=by, filters=filters, config=current_app.config)
        return ApiResult(results)
    except Exception as e:
        print(e)
        raise ApiException("COULD COUNT USERS", details=str(e))

@api.route("/access/counts/<what>", methods=["GET"])
@admin_required
def get_access_counts(what=None):
    try:
        by = request.args.get("by")
        filters = request.args.get("filter")
        results = AccessTracker.counts(what=what, by=by, filters=filters, config=current_app.config)
        return ApiResult(results)
    except Exception as e:
        print(e)
        raise ApiException("COULD COUNT ACCESS", details=str(e))

@api.route("/phenos/<pheno_id>/purge", methods=["DELETE"])
@admin_required
def purge_pheno(pheno_id):
    try: 
        result = Phenotype.purge(pheno_id, current_app.config)
        result["purged"] = True
        return ApiResult(result)
    except Exception as e:
        raise ApiException("COULD NOT PURGE PHENO", details=str(e))

@api.route("/get-uuid", methods=["GET"])
@admin_required
def get_uuid():
    return ApiResult({"uuid": str(uuid.uuid4())})

@api.route('/lz/<resource>', methods=["GET", "POST"], strict_slashes=False)
@api.route('/lz/<resource>/<path:path>', methods=["GET", "POST"], strict_slashes=False)
def get_api_annotations(resource, path=None):
    if resource == "ld-results":
        ldresp =  requests.get('http://portaldev.sph.umich.edu/api/v1/pair/LD/results/', params=request.args)
        if ldresp.status_code != 500:
            return ldresp.content
        else:
            empty = "{\"data\": {\"position2\": []}}"
            return empty
    elif resource == "ld":
        ldserver = current_app.config.get("LD_SERVER")
        if not ldserver:
            return Response("No LD Server Configured", status=503)
        ldurl =  ldserver + (path or "")
        ldresp = requests.get(ldurl, params=request.args)
        return Response(ldresp.content, mimetype=ldresp.headers.get('content-type'), status=ldresp.status_code)
    elif resource == "gene":
        return requests.get('http://portaldev.sph.umich.edu/api/v1/annotation/genes/', params=request.args).content
    elif resource == "recomb":
        return requests.get('http://portaldev.sph.umich.edu/api/v1/annotation/recomb/results/', params=request.args).content
    else:
        return "Not Found", 404

@api.route("/help", methods=["POST"])
def post_help():
    form_data = request.form
    user_email = form_data.get("user_email", current_user.email)
    user_fullname = form_data.get("user_fullname", current_user.full_name)
    user_message = form_data.get("message", None)
    from_page = form_data.get("from_page", None)
    if not user_message:
        raise ApiException("EMPTY MESSAGE") 
    try:
        get_notifier().send_user_feedback(user_email, user_fullname, user_message, from_page, 
            current_user)
        return ApiResult({"sent": True, "from_page": from_page})
    except Exception as e:
        raise ApiException("FAILED TO SEND MESSAGE", details=str(e)) 

@api.route("/notices", methods=["GET"])
def get_api_notices():
    query = get_query_info(request)
    notices = Notice.list_current(current_app.config, query)
    return ApiResult(notices)

@api.route("/notices/<notice_id>", methods=["GET"])
def get_api_notice(notice_id):
    query = get_query_info(request)
    notices = Notice.get(notice_id).as_object()
    return ApiResult(notices)

@api.route("/notices-all", methods=["GET"])
@admin_required
def get_api_notices_all():
    query = get_query_info(request)
    notices = Notice.list_all(current_app.config, query)
    return ApiResult(notices)

@api.route("/notices", methods=["POST"])
@admin_required
def add_notice():
    try:
        values = request.values.to_dict(flat=True)
        Notice.create(values)
        return ApiResult({"created": True})
    except Exception as e:
        raise ApiException("COULD NOT CREATE NOTICE", details=str(e))

@api.route("/notices/<notice_id>", methods=["POST"])
@admin_required
def update_notice(notice_id):
    try:
        values = request.values.to_dict(flat=True)
        Notice.update(notice_id, values)
        return ApiResult({"updated": True})
    except Exception as e:
        raise ApiException("COULD NOT UPDATE NOTICE", details=str(e))

class ApiResult(object):
    def __init__(self, value, status=200, header=None, request=None):
        self.value = value
        self.status = status
        self.header = header
        self.request = request

    def __set_paging_headers(self, value):
        if self.header is None:
            self.header = {}
        self.header["total_count"] = value.total_count
        self.header["filtered_count"] = value.filtered_count
        if not value.page:
            return
        self.header["limit"] = value.page.limit
        self.header["offset"] = value.page.offset
        self.header["pages"] = value.page_count()
        if self.request:
            next_page = value.next_page()
            if next_page:
                self.header["next"] = ApiResult.update_url_page(self.request,
                    next_page)
            prev_page = value.prev_page()
            if prev_page:
                self.header["prev"] = ApiResult.update_url_page(self.request,
                    prev_page)
            if "echo" in request.args:
                self.header["echo"] = re.sub(r'[\W]+', "", request.args.get("echo"))

    @staticmethod
    def update_url_page(request, page):
        args = request.args.copy()
        args["limit"] = page.limit
        args["offset"] = page.offset
        return '{}?{}'.format(request.path, url_encode(args))

    def to_response(self):
        data = self.value
        if isinstance(data, PagedResult):
            self.__set_paging_headers(data)
            data = data.results
        if self.header:
            data = {"header": self.header, "data": data}
        return Response(json.dumps(data),
            status=self.status,
            mimetype='application/json')

class ApiException(Exception):
    def __init__(self, message, status=400, details=None):
        self.message = message
        self.status = status
        self.details = details

    def to_result(self):
        result = {'error': self.message}
        if self.details:
            result["details"] = self.details
        return ApiResult(result, status=self.status)

