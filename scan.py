import re
import subprocess
import os
from config import Config

from db_connection import DBConnection
from doi_entry import DoiFactory
import logging

class Scan:
    # TODO: this only encompasses a few of the tags we end up scanning for
    # in more detail with user specified things later - and I think it's currently the screening
    # step. that's bad; let's review to ensure that the top level screen encompasses
    # all the strings currently in _get_collection_manager_names and
    # _get_scored_strings.
    config = Config()

    def __init__(self, doi_object=None, doi_string=None):
        self.collection_tag_regex = self._get_collection_tag_regex()
        self.text_directory = self.config.get_string('scan', 'scan_text_directory')
        if not os.path.exists(self.text_directory):
            os.mkdir(self.text_directory)
            logging.info(f"Created directory to store interpolated text files: {self.text_directory}")
        if doi_object is None and doi_string is None:
            raise NotImplementedError("Provide an object or a string")
        if doi_string is not None and doi_object is None:
            select_doi = f"""select * from dois where doi = '{doi_string}'"""
            doi_object = DoiFactory(select_doi).dois
            if len(doi_object) != 1:
                raise RecordNotFoundException(f"{select_doi}")
            else:
                doi_object = doi_object[0]

        doi_string = doi_object.doi
        sql = f"""select * from scans where doi = '{doi_string}'"""
        scan_db_results = DBConnection.execute_query(sql)
        if len(scan_db_results) == 1:
            # logging.debug(f"{scan_db_results}")
            self.doi_string = scan_db_results[0][0]
            self.textfile_path = scan_db_results[0][1]
            self.score = scan_db_results[0][2]
            self.broken_converter = scan_db_results[0][3]
            self.title = scan_db_results[0][4]
            self.doi_object = doi_object
        else:
            self._init_from_object(doi_object)
            self._write_to_db()

        self.found_lines = []  # not currently saved

    @classmethod
    def clear_db_entry(self, doi):
        sql = f"delete from scans where doi='{doi}'"
        DBConnection.execute_query(sql)
        sql = f"delete from found_scan_lines where doi='{doi}'"
        DBConnection.execute_query(sql)

    def _write_to_db(self, write_scan_lines=False, clear_existing_records=False):
        if clear_existing_records:
            Scan.clear_db_entry(self.doi_string)
        sql_insert = f"""replace into scans (doi, textfile_path,score,cannot_convert,title) VALUES (?,?,?,?,?)"""
        args = [self.doi_string,
                self.textfile_path,
                self.score,
                self.broken_converter,
                self.doi_object.get_title()]
        DBConnection.execute_query(sql_insert, args)
        if write_scan_lines and len(self.found_lines) > 0:
            for score_tuple in self.found_lines:
                sql_insert = f"""insert into found_scan_lines (doi, line, score, matched_string) VALUES (?,?,?,?)"""
                args = [self.doi_string,
                        score_tuple[0],
                        score_tuple[1],
                        score_tuple[2]]
                DBConnection.execute_query(sql_insert, args)

    def _init_from_object(self, doi_object):
        self.textfile_path = None
        self.broken_converter = None
        if doi_object.full_path is None:
            if doi_object.check_file() is False:
                raise FileNotFoundError(
                    f"Missing PDF for doi {doi_object.doi}. path would be {doi_object.generate_file_path()} title: {doi_object.get_title()}")

        self.doi_object = doi_object
        self.doi_string = doi_object.doi
        self.full_path = doi_object.full_path
        self.title = doi_object.get_title()
        self._convert_pdf()
        self.found_lines = []
        self.score = None

    def __lt__(self, other):
        return self.score < other.score

    def __str__(self):
        str = f"{self.score}   {self.doi_string}:({self.doi_object.get_journal()})  {self.doi_object.details['title'][0]}"
        return str

    def _run_converter(self):
        command = f"/usr/local/bin/pdftotext"
        pwd = os.getcwd()
        filename = self.doi_object.get_filename_from_doi_entry()
        txt_file = f"{self.text_directory}/{filename.strip('.pdf')}.txt"

        pdf_file = f"{pwd}/{self.doi_object.full_path}"
        subprocess.call([command, pdf_file, txt_file])

    def _convert_pdf(self, force=False):
        doi_basename = os.path.basename(self.doi_object.full_path)
        doi_basename = doi_basename.rsplit(".", 1)[0]
        doi_textfile = os.path.join(self.text_directory, doi_basename + ".txt")
        if self.broken_converter:
            return False
        if not os.path.exists(doi_textfile) or force is True:
            logging.warning(f"missing txt file, generating {doi_textfile}")
            self._run_converter()
            if not os.path.exists(doi_textfile):
                logging.error("PDF conversion failure; marking as failed and continuing.")
                self.broken_converter = True
                return False
        if os.path.exists(doi_textfile):
            self.textfile_path = doi_textfile
        return True

    @classmethod
    def _get_collection_tag_regex(cls):
        institution_root_name = eval(cls.config.get_string('scan_search_keys', 'institution_root_name'))
        collections_with_id_strings = cls.config.get_list('scan_search_keys', 'collections_with_id_strings')
        collection_tag_regex = f"(([ \(\[])+|^)(?i){institution_root_name}"
        for id in collections_with_id_strings:
            collection_tag_regex += f"({id})*"
        collection_tag_regex += "[: ]+[ ]*[0-9\-]+"
        return collection_tag_regex

    @classmethod
    def _get_collection_manager_names(cls):
        collection_manager_names = cls.config.get_list('scan_search_keys', 'collection_manager_names')
        all_name_variations = []
        for test_string, score in collection_manager_names:
            test_string = test_string.lower()
            # Case 1: last name only like 'shevock'
            if len(test_string.split()) == 1:
                all_name_variations.append((test_string, score))
                continue
            # Case 2: parsing full names with first, last, and middle 
            if len(test_string.split()) == 3:
                firstname, middlename, lastname = test_string.split()
                all_name_variations.append((f"{firstname} {middlename} {lastname}", score))
            else:
                firstname, lastname = test_string.split()
            first_letter = firstname[0]
            # Case 3: parsing initial names like 'D.H. Kavanaugh'
            if firstname.count('.') > 1:
                all_name_variations.append((f"{firstname} {lastname}", score))
                all_name_variations.append((f"{firstname}{lastname}", score))
                all_name_variations.append((f"{firstname[:-1]} {lastname}", score))
                continue
            # Case 4: parsing full names with first and last
            elif len(firstname) != 1 and len(firstname.replace('.', '')) != 1:
                all_name_variations.append((f"{firstname} {lastname}", score))
                all_name_variations.append((f"{first_letter}. {lastname}", 200))
                all_name_variations.append((f"{first_letter} {lastname}", 200))
            # Case 5: initial name like 'd catania' or 'd. catania'
            else:
                all_name_variations.append((f"{first_letter}. {lastname}", score))
                all_name_variations.append((f"{first_letter} {lastname}", score))
        return all_name_variations

    @classmethod
    def _get_scored_strings(cls):
        # Test that hypehens and colons are parsed correctly in the
        # reguar expression sets
        string_set_pre_reference = cls.config.get_list('scan_search_keys', 'scored_strings')
        return string_set_pre_reference

    @classmethod
    def get_regex_score_tuples(cls):
        retval = []
        collection_tag_regex = cls._get_collection_tag_regex()
        retval.append((collection_tag_regex, 300))
        for regex_tuple in Scan._get_scored_strings() + Scan._get_collection_manager_names():
            regex = regex_tuple[0].lower()
            retval.append((regex, regex_tuple[1]))

        return retval

    def scan(self, clear_existing_records=False):
        # logging.debug(f"Scanning: {self.textfile_path}")
        if self._convert_pdf() is False:
            # logging.warning(f"Missing PDF, not scanning: {self.textfile_path}")
            self._write_to_db()
            return False
        if clear_existing_records or self.score is None:
            self.score = 0
        regex = self.collection_tag_regex
        results = self._scan_with_regex(regex, 300, ok_after_references=True, do_score=False)
        for result in results:
            hyphen_count = result.count('-')
            if hyphen_count < 2:
                # logging.debug(f"Hyphens ok: {result}")
                self.score += 300
            else:
                # logging.debug(f"Hyphens bad: {result}")
                self.score -= 20

        collection_manager_names = Scan._get_collection_manager_names()

        string_set_pre_reference = Scan._get_scored_strings()
        string_set_pre_reference = string_set_pre_reference + collection_manager_names
        self._scan_keywords(string_set_pre_reference, ok_after_references=False)

        string_set_post_reference = collection_manager_names

        self._scan_keywords(string_set_post_reference, ok_after_references=True)

        if self.score > 0:
            logging.info(f"{self.score}\t{self.title}")
        self._write_to_db(write_scan_lines=True, clear_existing_records=clear_existing_records)

        return True

    def _scan_keywords(self, string_set, ok_after_references=False):
        for test_string, score in string_set:
            test_string = test_string.lower()
            # logging.info(f"Scanning test string: {test_string}")
            regex = f"(([ \(\[])+|^)(?i){test_string}(([ ,.:])+|$)"
            regex_result_count = self._scan_with_regex(regex, score, ok_after_references)
            # if regex_result_count > 0:
            #     logging.info(f"Found: {test_string} Score: {self.score}")

    def scan_specimen_ids(self):
        if self.score is None:
            self.score = 0
        regex = self.collection_tag_regex

        return self._scan_with_regex(regex, 1, False)

    def _scan_with_regex(self, regex, score_per_line, ok_after_references, do_score=True):
        results = []

        # logging.debug(f"Scanning with regex: {regex}")
        found_count = 0
        cur_line = None
        with open(self.textfile_path, "r") as a_file:
            for next_line in a_file:
                next_line = next_line.lower()
                next_line = next_line.strip()
                next_words = next_line.split()

                if len(next_words) <= 1:
                    try:
                        if (len(next_words) > 0 and len(next_words[0]) <= 1) or \
                                (len(next_words) > 0 and next_words[0].isdigit() and int(next_words[0]) < 1000):
                            # This is usually a line number or a blank line.
                            # logging.debug(f"Skipping blank: {next_line}")
                            continue
                    except ValueError as e:
                        logging.error(f"Bad line value, not breaking. {e}")

                if len(next_words) == 0:
                    # logging.info(f"totally blank: {next_line}")
                    continue
                if cur_line is not None:
                    cur_line = cur_line.strip()
                    # append three words of the next line
                    hyphen = False
                    if cur_line.endswith('-'):
                        hyphen = True
                        cur_line = cur_line[:-1]
                    for id, next_word in enumerate(next_words):
                        if id > 3:
                            # For longer key phrases. e.g.: "califnroia academy of sciences"
                            # broken up across several lines.
                            # False positives are ok, false negatives are no fun.
                            break
                        if not hyphen:
                            cur_line = cur_line + f" {next_words[id]}"
                        else:
                            cur_line = cur_line + f"{next_words[id]}"
                            hyphen = False

                    result = re.search(regex, cur_line)
                    if result is not None:
                        # logging.debug(".", end='')
                        # logging.debug(f"{self.textfile_path} possible: {cur_line}")
                        results.append(result.group(0))
                        found_count += 1
                        self.found_lines.append((cur_line, score_per_line, result.group(0)))
                cur_line = next_line
                if ok_after_references is False:
                    if next_line == "references" in next_line or \
                            "referencias" in next_line or \
                            "ЛИТЕРАТУРА".lower() in next_line or \
                            "literature cited" in next_line:
                        # logging.debug("Stopping scan before references.")
                        break
        old_score = self.score
        assert self.score is not None

        if do_score:
            self.score = self.score + (score_per_line * found_count)
        # if found_count > 0:
        #     logging.debug(f"Score change. From {old_score} to {self.score}\n")
        return results


class RecordNotFoundException(Exception):
    pass
