#!/usr/bin/env python3

""" patent_xml_to_csv.py """

import argparse
import csv
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from functools import partial
from io import BytesIO
from pathlib import Path
from pprint import pformat

import yaml
from lxml import etree


assert sys.version_info >= (3, 6), "Error: Python 3.6 or newer is required."

if sys.version_info < (3, 7):
    try:
        from multiprocess import Pool, cpu_count
    except ImportError:
        sys.exit(
            "Error: If running with Python < 3.7, the multiprocess library is required "
            "(e.g. pip install multiprocess)."
        )
else:
    from multiprocessing import Pool, cpu_count


try:
    from termcolor import colored
except ImportError:
    logging.debug("termcolor (pip install termcolor) not available")

    def colored(text, _color):
        """ Dummy function in case termcolor is not available. """
        return text


def replace_missing_ents(doc):
    """
    Substitute out some undefined entities that appear in the XML

    * IndentingNewLine
    * LeftBracketingBar
    * LeftDoubleBracketingBar
    * RightBracketingBar

      These seem to be MathML symbols, but the mappings that I've used (deriving from,
      e.g., https://reference.wolfram.com/language/ref/character/LeftBracketingBar.html
      and http://www.mathmlcentral.com/characters/glyphs/LeftBracketingBar.html) point to
      code points in the PUA of the Unicode BMP -- i.e., they're only going to work with
      specific fonts.

      It seems like they should be part of mmlextra (see
      https://www.w3.org/TR/REC-MathML/chap6/byalpha.html), but they're not in any of the
      versions (plural!) of this file that I have available, or can find documented
      online (see, e.g., https://www.w3.org/TR/MathML2/mmlextra.html,
      https://www.w3.org/2003/entities/mathmldoc/mmlextra.html etc.)

      Alternative renderings are included in mmlalias.ent -- unclear where these come
      from.

      The version of mmlextra.ent from here:
      https://github.com/martinklepsch/patalyze/blob/master/resources/parsedir/mmlextra.ent
      seems to have what's required, and uses the PUA renderings in line with
      mathmlcentral.com and reference.wolfram.com

    ----

    * LeftSkeleton
    * RightSkeleton

      Another MathML symbol, but there is a discrepancy here in that
      https://reference.wolfram.com/ and https://www.mathmlcentral.com/ use U+F761 and
      U+F762 for LeftSkeleton and RightSkeleton respectively, and my copy of mmlextra.ent
      uses U+E850 and U+E851.

    ----

    * hearts

      This entity appears in the detailed description for, e.g., 09489911 (note that this
      description is not extracted by the parser according to the current version of the
      config file at config/uspto-grants-0105.yaml).

      In the same context the suit "diamonds" is represented by &diams;, and clubs and
      hearts are presented by <CUSTOM-CHARACTER> elements which reference external TIFF
      files... (sigh).  This is despite the face that the appropriate XML entities
      (&hearts;, &diams;, &clubs;, and &spades;) are all defined in the DTDs and .ent
      files available, but for some reason &hearts; (and &hearts; alone) is missing from
      isopub.ent, which is the file actually invoked by the DTD specified in the XML
      (double sigh).

      In some of the DTD files the symbols specified for the suits are the white glpyhs
      (i.e. &#x2661, &#x2662, &#x2664, and &#x2667 for hearts, diamonds, spades and clubs
      respectively), and in others they are the black glyphs (i.e. &#x2665, &#x2666,
      &#x2660, and &#x2663) -- see, e.g.
      https://en.wikipedia.org/wiki/List_of_Unicode_characters#Miscellaneous_Symbols.

      I've chose the black variant here, as the black variants are used in isopub.ent
      -- but note that Google Patents has used the black variants for diamonds
      (presumably from isopub.ent), the white variant for hearts (coopted from another
      DTD?), and has dropped the <CUSTOM-CHARACTER> elements for spades and clubs (see
      https://patents.google.com/patent/US6612926).

    """

    doc = doc.replace("&IndentingNewLine;", "&#xF3A3;")
    doc = doc.replace("&LeftBracketingBar;", "&#xF603;")
    doc = doc.replace("&RightBracketingBar;", "&#xF604;")
    doc = doc.replace("&LeftDoubleBracketingBar;", "&#xF605;")
    doc = doc.replace("&RightDoubleBracketingBar;", "&#xF606;")

    doc = doc.replace("&LeftSkeleton;", "&#xF761;")
    doc = doc.replace("&RightSkeleton;", "&#xF762;")

    doc = doc.replace("&hearts;", "&#x2665;")
    return doc


def expand_paths(path_expr):
    path = Path(path_expr).expanduser()
    return Path(path.root).glob(
        str(Path("").joinpath(*path.parts[1:] if path.is_absolute() else path.parts))
    )


class DTDResolver(etree.Resolver):
    def __init__(self, dtd_path):
        self.dtd_path = Path(dtd_path)

    def resolve(self, system_url, _public_id, context):
        if system_url.startswith(str(self.dtd_path)):
            return self.resolve_filename(system_url, context)
        else:
            return self.resolve_filename(
                str((self.dtd_path / system_url).resolve()), context
            )


class XmlDocToTabular:
    def __init__(self, logger, config, dtd_path, validate, continue_on_error):
        self.logger = logger
        self.config = config
        self.dtd_path = dtd_path
        self.validate = validate
        self.continue_on_error = continue_on_error
        self.tables = defaultdict(list)
        # lambdas can't be pickled (without dill, at least)
        self.table_pk_idx = defaultdict(partial(defaultdict, int))

    @staticmethod
    def get_text(xpath_result):
        if isinstance(xpath_result, str):
            return re.sub(r"\s+", " ", xpath_result).strip()
        return re.sub(
            r"\s+", " ", etree.tostring(xpath_result, method="text", encoding="unicode")
        ).strip()

    def get_pk(self, tree, config):
        if "<primary_key>" in config:
            elems = tree.findall("./" + config["<primary_key>"])
            assert len(elems) == 1
            return self.get_text(elems[0])
        return None

    def add_string(self, path, elems, record, fieldname):
        try:
            assert len(elems) == 1
        except AssertionError as exc:
            exc.msg = (
                f"Multiple elements found for {path}! "
                + "Should your config file include a joiner, or new entity "
                + "definition?"
                + "\n\n- "
                + "\n- ".join(self.get_text(el) for el in elems)
            )
            raise

        # we've only one elem, and it's a simple mapping to a fieldname
        record[fieldname] = self.get_text(elems[0])

    def process_doc(self, payload):
        filename, linenum, doc = payload

        try:
            tree = self.parse_tree(doc)
            for path, config in self.config.items():
                self.process_path(tree, path, config, filename, {})

        except LookupError as exc:
            self.logger.warning(exc.args[0])
            if not self.continue_on_error:
                raise SystemExit()

        except etree.XMLSyntaxError as exc:
            self.logger.debug(doc)
            self.logger.warning(
                colored(
                    "Unable to parse XML document ending at line %d "
                    "(enable debugging -v to dump doc to console):\n\t%s",
                    "red",
                ),
                linenum,
                exc.msg,
            )
            if not self.continue_on_error:
                raise SystemExit()

        except AssertionError as exc:
            self.logger.debug(doc)
            pk = self.get_pk(self.parse_tree(doc), next(iter(self.config.values())))
            self.logger.warning(
                colored("Record ID %s @%d: (record has not been parsed)", "red"),
                pk,
                linenum,
            )
            self.logger.warning(exc.msg)
            if not self.continue_on_error:
                raise SystemExit()

        return self.tables

    def parse_tree(self, doc):
        doc = replace_missing_ents(doc)

        parser_args = {
            "load_dtd": True,
            "resolve_entities": True,
            "ns_clean": True,
            "huge_tree": True,
            "collect_ids": False,
        }

        if self.validate:
            parser_args["dtd_validation"] = True

        parser = etree.XMLParser(**parser_args)
        parser.resolvers.add(DTDResolver(self.dtd_path))
        return etree.parse(BytesIO(doc.encode("utf8")), parser)

    def process_path(
        self, tree, path, config, filename, record, parent_entity=None, parent_pk=None
    ):
        try:
            elems = [tree.getroot()]
        except AttributeError:
            elems = tree.xpath("./" + path)

        self.process_field(
            elems, tree, path, config, filename, record, parent_entity, parent_pk
        )

    def process_field(
        self,
        elems,
        tree,
        path,
        config,
        filename,
        record,
        parent_entity=None,
        parent_pk=None,
    ):

        if isinstance(config, str):
            if elems:
                self.add_string(path, elems, record, config)
            return

        if "<entity>" in config:
            # config is a new entity definition (i.e. a new record on a new table/file)
            self.process_new_entity(
                tree, elems, config, filename, parent_entity, parent_pk
            )
            return

        if "<fieldname>" in config:
            # config is extra configuration for a field on this table/file
            if "<joiner>" in config:
                if elems:
                    record[config["<fieldname>"]] = config["<joiner>"].join(
                        [self.get_text(elem) for elem in elems]
                    )
                return

            if "<enum_map>" in config:
                if elems:
                    record[config["<fieldname>"]] = config["<enum_map>"].get(
                        self.get_text(elems[0])
                    )
                return

            if "<enum_type>" in config:
                if elems:
                    record[config["<fieldname>"]] = config["<enum_type>"]
                return

            # just a mapping to a fieldname string
            if len(config) == 1:
                self.add_string(path, elems, record, config["<fieldname>"])
                return

        # We may have multiple configurations for this key (XPath expression)
        if isinstance(config, list):
            for subconfig in config:
                self.process_field(
                    elems, tree, path, subconfig, filename, record, parent_entity
                )
            return

        raise LookupError(
            f'Invalid configuration for key "{parent_entity}":'
            + "\n "
            + "\n ".join(pformat(config).split("\n"))
        )

    def process_new_entity(
        self, tree, elems, config, filename, parent_entity=None, parent_pk=None
    ):
        """Process a subtree of the xml as a new entity type, creating a new record in a
        new output table/file.
        """
        entity = config["<entity>"]
        for elem in elems:
            record = {}

            pk = self.get_pk(tree, config)
            if pk:
                record["id"] = pk
            else:
                record["id"] = f"{parent_pk}_{self.table_pk_idx[entity][parent_pk]}"
                self.table_pk_idx[entity][parent_pk] += 1

            if parent_pk:
                record[f"{parent_entity}_id"] = parent_pk
            if "<filename_field>" in config:
                record[config["<filename_field>"]] = filename
            for subpath, subconfig in config["<fields>"].items():
                self.process_path(
                    elem, subpath, subconfig, filename, record, entity, pk
                )

            self.tables[entity].append(record)


class XmlCollectionToTabular:
    def __init__(
        self, xml_input, config, dtd_path, output_path, output_type, logger, **kwargs
    ):

        self.logger = logger

        self.xml_files = []
        for input_path in xml_input:
            for path in expand_paths(input_path):
                if path.is_file():
                    self.xml_files.append(path)
                elif path.is_dir():
                    self.xml_files.extend(
                        path.glob(f'{"**/" if kwargs["recurse"] else ""}*.[xX][mM][lL]')
                    )
                else:
                    self.logger.fatal("specified input is invalid")
                    exit(1)

        # do this now, because we don't want to process all that data and then find
        #  the output_path is invalid... :)
        self.output_path = Path(output_path)
        self.output_path.mkdir(parents=True, exist_ok=True)

        self.output_type = output_type

        if self.output_type == "sqlite":
            try:
                from sqlite_utils import Database as SqliteDB  # noqa

                self.db_path = (self.output_path / "db.sqlite").resolve()
                if self.db_path.exists():
                    self.logger.warning(
                        colored(
                            "Sqlite database %s exists; records will be appended.",
                            "yellow",
                        ),
                        self.db_path,
                    )

                db_conn = sqlite3.connect(str(self.db_path), isolation_level=None)
                db_conn.execute("pragma synchronous=off;")
                db_conn.execute("pragma journal_mode=memory;")
                self.db = SqliteDB(db_conn)

            except ImportError:
                logger.debug("sqlite_utils (pip3 install sqlite-utils) not available")
                raise

        self.config = yaml.safe_load(open(config))

        self.dtd_path = dtd_path
        self.validate = kwargs["validate"]
        self.processes = kwargs["processes"]
        self.continue_on_error = kwargs["continue_on_error"]

        self.fieldnames = self.get_fieldnames()
        self.get_root_config()

    def yield_xml_doc(self, filepath):
        filename = filepath.resolve().name
        xml_doc = []
        with open(filepath, "r", errors="replace") as _fh:
            for i, line in enumerate(_fh):
                if line.startswith("<?xml "):
                    try:
                        if xml_doc and xml_doc[1].startswith(
                            f"<!DOCTYPE {self.xml_root}"
                        ):
                            yield (filename, i - len(xml_doc), "".join(xml_doc))
                    except Exception as exc:
                        self.logger.warning(exc.args[0])
                        self.logger.debug(
                            "Unexpected XML document at line %d:\n%s", i, xml_doc
                        )
                        if not self.continue_on_error:
                            raise SystemExit()
                    xml_doc = []
                xml_doc.append(line)

            if xml_doc and xml_doc[1].startswith(f"<!DOCTYPE {self.xml_root}"):
                yield (filename, i - len(xml_doc), "".join(xml_doc))

    def get_root_config(self):
        self.xml_root = self.config.get("xml_root", None)
        if self.xml_root is None:
            self.xml_root = next(iter(self.config.keys()))
            self.logger.warning(
                colored(
                    "<xml_root> not explicitly set in config -- assuming <%s/>",
                    "yellow",
                )
                % self.xml_root
            )

    def convert(self):
        if not self.xml_files:
            self.logger.warning(colored("No input files to process!", "red"))

        docParser = XmlDocToTabular(
            self.logger,
            self.config,
            self.dtd_path,
            self.validate,
            self.continue_on_error,
        )

        for input_file in self.xml_files:

            self.logger.info(colored("Processing %s...", "green"), input_file.resolve())

            processes = self.processes or cpu_count() - 1 or 1
            # chunk sizes greater than 1 result in duplicate returns because the results
            #  are pooled on the XmlDocToTabular instance
            chunksize = 1

            pool = Pool(processes=processes)

            all_tables = defaultdict(list)
            for i, tables in enumerate(
                pool.imap(
                    docParser.process_doc,
                    self.yield_xml_doc(input_file),
                    chunksize,
                )
            ):

                if i % 100 == 0:
                    self.logger.debug(
                        colored("Processing document %d...", "cyan"), i + 1
                    )
                for key, value in tables.items():
                    all_tables[key].extend(value)

            pool.close()
            pool.join()

            if tables:
                self.logger.info(colored("...%d records processed!", "green"), i + 1)
                self.flush_to_disk(all_tables)
            else:
                self.logger.warning(
                    colored("No records found! (config file error?)", "red")
                )

    def flush_to_disk(self, tables):
        if self.output_type == "csv":
            self.write_csv_files(tables)

        if self.output_type == "sqlite":
            self.write_sqlitedb(tables)

    def get_fieldnames(self):
        """
        On python >=3.7, dictionaries maintain key order, so fields are guaranteed to
        be returned in the order in which they appear in the config file.  To guarantee
        this on versions of python <3.7 (insofar as it matters), collections.OrderedDict
        would have to be used here.
        """

        fieldnames = defaultdict(list)

        def add_fieldnames(config, _fieldnames, parent_entity=None):
            if isinstance(config, str):
                if ":" in config:
                    _fieldnames.append(config.split(":")[0])
                    return
                _fieldnames.append(config)
                return

            if "<fieldname>" in config:
                _fieldnames.append(config["<fieldname>"])
                return

            if "<entity>" in config:
                entity = config["<entity>"]
                _fieldnames = []
                if "<primary_key>" in config or parent_entity:
                    _fieldnames.append("id")
                if parent_entity:
                    _fieldnames.append(f"{parent_entity}_id")
                if "<filename_field>" in config:
                    _fieldnames.append(config["<filename_field>"])
                for subconfig in config["<fields>"].values():
                    add_fieldnames(subconfig, _fieldnames, entity)
                # different keys (XPath expressions) may be appending rows to the same
                #  table(s), so we're appending to lists of fieldnames here.
                fieldnames[entity] = list(
                    dict.fromkeys(fieldnames[entity] + _fieldnames).keys()
                )
                return

            # We may have multiple configurations for this key (XPath expression)
            if isinstance(config, list):
                for subconfig in config:
                    add_fieldnames(subconfig, _fieldnames, parent_entity)
                return

            raise LookupError(
                "Invalid configuration:"
                + "\n "
                + "\n ".join(pformat(config).split("\n"))
            )

        for key, config in self.config.items():
            if key.startswith("<"):
                # skip keyword instructions
                continue
            add_fieldnames(config, [])

        return fieldnames

    def write_csv_files(self, tables):

        self.logger.info(
            colored("Writing csv files to %s ...", "green"), self.output_path.resolve()
        )
        for tablename, rows in tables.items():
            output_file = self.output_path / f"{tablename}.csv"

            if output_file.exists():
                self.logger.debug(
                    colored("CSV file %s exists; records will be appended.", "yellow"),
                    output_file,
                )

                with output_file.open("a") as _fh:
                    writer = csv.DictWriter(_fh, fieldnames=self.fieldnames[tablename])
                    writer.writerows(rows)

            else:
                with output_file.open("w") as _fh:
                    writer = csv.DictWriter(_fh, fieldnames=self.fieldnames[tablename])
                    writer.writeheader()
                    writer.writerows(rows)

    def write_sqlitedb(self, tables):
        self.logger.info(colored("Writing records to %s ...", "green"), self.db_path)
        self.db.conn.execute("begin exclusive;")
        for tablename, rows in tables.items():
            params = {"column_order": self.fieldnames[tablename], "alter": True}
            if "id" in self.fieldnames[tablename]:
                params["pk"] = "id"
                params["not_null"] = {"id"}
            self.logger.debug(
                colored("Writing %d records to `%s`...", "magenta"),
                len(rows),
                tablename,
            )
            self.db[tablename].insert_all(rows, **params)


def main():
    """ Command-line entry-point. """
    arg_parser = argparse.ArgumentParser(description="Description: {}".format(__file__))

    arg_parser.add_argument(
        "-v", "--verbose", action="store_true", default=False, help="increase verbosity"
    )
    arg_parser.add_argument(
        "-q", "--quiet", action="store_true", default=False, help="quiet operation"
    )

    arg_parser.add_argument(
        "-i",
        "--xml-input",
        action="store",
        nargs="+",
        required=True,
        help="XML file or directory of XML files (*.{xml,XML}) to parse recursively"
        " (multiple arguments can be passed)",
    )

    arg_parser.add_argument(
        "-r",
        "--recurse",
        action="store_true",
        help="if supplied, the parser will search subdirectories for"
        " XML files (*.{xml,XML}) to parse",
    )

    arg_parser.add_argument(
        "-c",
        "--config",
        action="store",
        required=True,
        help="config file (in YAML format)",
    )

    arg_parser.add_argument(
        "-d",
        "--dtd-path",
        action="store",
        required=True,
        help="path to folder where dtds and related documents can be found",
    )

    arg_parser.add_argument(
        "--validate",
        action="store_true",
        help="skip validation of input XML (for speed)",
    )

    arg_parser.add_argument(
        "-o",
        "--output-path",
        action="store",
        required=True,
        help="path to folder in which to save output (will be created if necessary)",
    )

    arg_parser.add_argument(
        "--output-type",
        choices=["csv", "sqlite"],
        action="store",
        default="csv",
        help="output csv files (one per table, default) or a sqlite database",
    )

    arg_parser.add_argument(
        "--processes",
        action="store",
        type=int,
        help="number of processes to use for parallel processing of XML documents"
        " (defaults to num_threads - 1)",
    )

    arg_parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="output errors on parsing failure but don't exit",
    )

    args = arg_parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_level = logging.CRITICAL if args.quiet else log_level
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)
    logger.addHandler(logging.StreamHandler())

    convertor = XmlCollectionToTabular(**vars(args), logger=logger)
    convertor.convert()


if __name__ == "__main__":
    main()
