import os
import argparse
from time import time
from time import sleep
from sqlalchemy import sql
from subprocess import call
import heroku3


from app import db
from jobs import update_registry
import jobs_defs # needs to be imported so the definitions get loaded into the registry
from util import elapsed


def run_sql(q):
    q = q.strip()
    if not q:
        return
    print "running {}".format(q)
    start = time()
    con = db.engine.connect()
    trans = con.begin()
    con.execute(q)
    trans.commit()
    con.close()
    print "{} done in {} seconds".format(q, elapsed(start, 1))

def get_sql_answer(q):
    row = db.engine.execute(sql.text(q)).first()
    return row[0]

def print_status():
    num_dois = get_sql_answer("select count(id) from doi_queue")
    num_waiting = get_sql_answer("select count(id) from doi_queue where enqueued=false")
    print u"There are {} dois in the queue, of which {} ({}%) are waiting to run".format(
        num_dois, num_waiting, int(100*float(num_waiting)/num_dois))

def reset_enqueued():
    q = u"update doi_queue set enqueued=false"
    run_sql(q)
    print_status()

def truncate():
    q = "truncate table doi_queue"
    run_sql(q)

def num_dynos(process_name):
    heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
    try:
        dynos = heroku_conn.apps()["oadoi"].dynos()[process_name]
    except KeyError:
        dynos = []
    return len(dynos)

def scale_dyno(n):
    process_name = "run_all" # formation name is from Procfile

    print "starting with {} dynos".format(num_dynos(process_name))
    print "setting to {} dynos".format(n)
    heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
    app = heroku_conn.apps()['oadoi']
    app.process_formation()[process_name].scale(n)

    print "sleeping for 2 seconds while it kicks in"
    sleep(2)
    print "verifying: now at {} dynos".format(num_dynos(process_name))

def export():
    num_dois = get_sql_answer("select count(doi) from export_queue")
    print u"There are {} dois ready to be exported".format(num_dois)

    command = """psql `heroku config:get DATABASE_URL`?ssl=true -c "\copy (select * from export_queue) to 'data/export_queue.csv' WITH (FORMAT CSV, HEADER);" """
    call(command, shell=True)

    command = """gzip -c data/export_queue.csv > data/export_queue.csv.gz;"""
    call(command, shell=True)

    command = """aws s3 cpdata/export_queue.csv.gz s3://oadoi-export/data/export_queue.csv.gz;"""
    call(command, shell=True)

    print "now go to https://console.aws.amazon.com/s3/buckets/oadoi-export/?region=us-east-1&tab=overview"


def add_dois_to_queue(filename):
    start = time()

    command = """psql `heroku config:get DATABASE_URL`?ssl=true -c "\copy doi_queue (id) FROM '{}' WITH CSV DELIMITER E'|';" """.format(
        filename)
    call(command, shell=True)

    q = "update doi_queue set id=lower(id)"
    run_sql(q)

    print "add_dois_to_queue done in {} seconds".format(elapsed(start, 1))
    print_status()


def add_all_dois_to_queue():
    print "adding all dois, this may take a while"
    start = time()
    recreate_commands = """
        drop table doi_queue cascade;
        CREATE TABLE doi_queue as (select id, random() as rand, false as enqueued from crossref);
        alter table doi_queue alter column rand set default random();
        alter table doi_queue alter column enqueued set default false;
        CREATE INDEX doi_queue_enqueued_idx ON doi_queue USING btree (enqueued);
        CREATE INDEX doi_queue_rand_enqueued_idx ON doi_queue USING btree (rand, enqueued);
        CREATE INDEX doi_queue_rand_idx ON doi_queue USING btree (rand);
        CREATE INDEX doi_queue_id_idx ON doi_queue USING btree (id);"""
    for command in recreate_commands.split(";"):
        run_sql(command)

    command = """create view export_queue as
     SELECT crossref.id AS doi,
        crossref.response_with_hybrid ->> 'evidence'::text AS evidence,
        crossref.response_with_hybrid ->> 'oa_color_long'::text AS oa_color,
        crossref.response_with_hybrid ->> 'free_fulltext_url'::text AS best_open_url,
        crossref.response_with_hybrid ->> 'year'::text AS year,
        crossref.response_with_hybrid ->> 'found_hybrid'::text AS found_hybrid,
        crossref.response_with_hybrid ->> 'found_green'::text AS found_green,
        crossref.response_with_hybrid ->> 'error'::text AS error,
        crossref.response_with_hybrid ->> 'is_boai_license'::text AS is_boai_license,
        replace((crossref.api -> '_source'::text) ->> 'journal'::text, '
    '::text, ''::text) AS journal,
        replace((crossref.api -> '_source'::text) ->> 'publisher'::text, '
    '::text, ''::text) AS publisher,
        (crossref.api -> '_source'::text) ->> 'subject'::text AS subject,
        crossref.response_with_hybrid ->> 'green_base_collections'::text AS green_base_collections,
        crossref.response_with_hybrid ->> 'license'::text AS license
       FROM crossref, doi_queue WHERE crossref.id = doi_queue.id and doi_queue.enqueued=true"""
    run_sql(command)

    # they are already lowercased
    print "add_all_dois_to_queue done in {} seconds".format(elapsed(start, 1))
    print_status()



def run_with_hybrid(parsed_args):
    start = time()
    update = update_registry.get("Crossref.run_with_realtime_scraping")
    update.run(**vars(parsed_args))
    print "finished update in {} seconds".format(elapsed(start))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run stuff.")
    parser.add_argument('--filename', nargs="?", type=str, help="filename with dois, one per line")
    parser.add_argument('--limit', "-l", nargs="?", type=int, help="how many jobs to do")
    parser.add_argument('--chunk', "-ch", nargs="?", default=10, type=int, help="how many to take off db at once")
    parser.add_argument('--id', nargs="?", type=str, help="id of the one thing you want to update")

    parser.add_argument('--addall', default=False, action='store_true', help="do you want to just reset?")
    parser.add_argument('--reset', default=False, action='store_true', help="do you want to just reset?")
    parser.add_argument('--run', default=False, action='store_true', help="to run the queue")
    parser.add_argument('--status', default=False, action='store_true', help="to print the status")
    parser.add_argument('--dynos', default=None, type=int, help="scale to this many dynos")
    parser.add_argument('--export', default=False, action='store_true', help="export the results")
    parsed_args = parser.parse_args()

    if parsed_args.filename:
        truncate()
        add_dois_to_queue(parsed_args.filename)

    if parsed_args.dynos != None:  # to tell the difference from setting to 0
        scale_dyno(parsed_args.dynos)

    if parsed_args.addall:
        truncate()
        add_all_dois_to_queue()

    if parsed_args.reset:
        reset_enqueued()

    if parsed_args.status:
        print_status()

    if parsed_args.export:
        export()

    # @todo either call run_with_hybrid or run_no_hybrid
    if parsed_args.run:
        run_with_hybrid(parsed_args)


