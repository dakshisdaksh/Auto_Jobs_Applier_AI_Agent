import base64
from calendar import c
import json
from math import log
from operator import is_
import os
import random
import re
import time
import traceback
from typing import List, Optional, Any, Text, Tuple

from httpx import HTTPStatusError
from regex import W
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from reportlab.pdfbase.pdfmetrics import stringWidth
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from jobContext import JobContext
from job_application import JobApplication
from job_application_saver import ApplicationSaver
from job_portals.application_form_elements import RadioQuestion, TextBoxQuestionType
from job_portals.base_job_portal import BaseJobPage, BaseJobPortal
import src.utils as utils
from src.logging import logger
from src.job import Job
from src.ai_hawk.llm.llm_manager import GPTAnswerer
from utils import browser_utils
import utils.time_utils


def question_already_exists_in_data(question: str, data: List[dict]) -> bool:
    """
    Check if a question already exists in the data list.

    Args:
        question: The question text to search for
        data: List of question dictionaries to search through

    Returns:
        bool: True if question exists, False otherwise
    """
    return any(item["question"] == question for item in data)


class AIHawkEasyApplier:
    def __init__(
        self,
        job_portal: BaseJobPortal,
        resume_dir: Optional[str],
        set_old_answers: List[Tuple[str, str, str]],
        gpt_answerer: GPTAnswerer,
        resume_generator_manager,
    ):
        logger.debug("Initializing AIHawkEasyApplier")
        if resume_dir is None or not os.path.exists(resume_dir):
            resume_dir = None
        self.job_page = job_portal.job_page
        self.job_application_page = job_portal.application_page
        self.resume_path = resume_dir
        self.set_old_answers = set_old_answers
        self.gpt_answerer = gpt_answerer
        self.resume_generator_manager = resume_generator_manager
        self.all_data = self._load_questions_from_json()
        self.current_job = None

        logger.debug("AIHawkEasyApplier initialized successfully")

    def _load_questions_from_json(self) -> List[dict]:
        output_file = "answers.json"
        logger.debug(f"Loading questions from JSON file: {output_file}")
        try:
            with open(output_file, "r") as f:
                try:
                    data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError(
                            "JSON file format is incorrect. Expected a list of questions."
                        )
                except json.JSONDecodeError:
                    logger.error("JSON decoding failed")
                    data = []
            logger.debug("Questions loaded successfully from JSON")
            return data
        except FileNotFoundError:
            logger.warning("JSON file not found, returning empty list")
            return []
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Error loading questions data from JSON file: {tb_str}")
            raise Exception(
                f"Error loading questions data from JSON file: \nTraceback:\n{tb_str}"
            )

    def apply_to_job(self, job: Job) -> None:
        """
        Starts the process of applying to a job.
        :param job: A job object with the job details.
        :return: None
        """
        logger.debug(f"Applying to job: {job}")
        try:
            self.job_apply(job)
            logger.info(f"Successfully applied to job: {job.title}")
        except Exception as e:
            logger.error(f"Failed to apply to job: {job.title}, error: {str(e)}")
            raise e

    def job_apply(self, job: Job):
        logger.debug(f"Starting job application for job: {job}")
        job_context = JobContext()
        job_context.job = job
        job_context.job_application = JobApplication(job)
        self.job_page.goto_job_page(job)

        try:

            job_description = self.job_page.get_job_description(job)
            logger.debug(f"Job description set: {job_description[:100]}")

            job.set_job_description(job_description)

            recruiter_link = self.job_page.get_recruiter_link()
            job.set_recruiter_link(recruiter_link)

            self.current_job = job

            logger.debug("Passing job information to GPT Answerer")
            self.gpt_answerer.set_job(job)

            # Todo: add this job to skip list with it's reason
            if not self.gpt_answerer.is_job_suitable():
                return

            self.job_page.click_apply_button(job_context)

            logger.debug("Filling out application form")
            self._fill_application_form(job_context)
            logger.debug(
                f"Job application process completed successfully for job: {job}"
            )

        except Exception as e:

            tb_str = traceback.format_exc()
            logger.error(f"Failed to apply to job: {job}, error: {tb_str}")

            logger.debug("Saving application process due to failure")
            self._save_job_application_process()

            raise Exception(
                f"Failed to apply to job! Original exception:\nTraceback:\n{tb_str}"
            )

    def _fill_application_form(self, job_context: JobContext):
        job = job_context.job
        job_application = job_context.job_application
        logger.debug(f"Filling out application form for job: {job}")

        self.fill_up(job_context)

        while self.job_application_page.has_next_button():
            self.fill_up(job_context)
            self.job_application_page.click_next_button()
            self.job_application_page.handle_errors()

        if self.job_application_page.has_submit_button():
            self.job_application_page.click_submit_button()
            ApplicationSaver.save(job_application)
            logger.debug("Application form submitted")
            return

        logger.warning(f"submit button not found, discarding application {job}")

    def _discard_application(self) -> None:
        logger.debug("Discarding application")
        try:
            self.driver.find_element(By.CLASS_NAME, "artdeco-modal__dismiss").click()
            utils.time_utils.medium_sleep()
            self.driver.find_elements(
                By.CLASS_NAME, "artdeco-modal__confirm-dialog-btn"
            )[0].click()
            utils.time_utils.medium_sleep()
        except Exception as e:
            logger.warning(f"Failed to discard application: {e}")

    def _save_job_application_process(self) -> None:
        logger.debug(
            "Application not completed. Saving job to My Jobs, In Progess section"
        )
        try:
            self.driver.find_element(By.CLASS_NAME, "artdeco-modal__dismiss").click()
            utils.time_utils.medium_sleep()
            self.driver.find_elements(
                By.CLASS_NAME, "artdeco-modal__confirm-dialog-btn"
            )[1].click()
            utils.time_utils.medium_sleep()
        except Exception as e:
            logger.error(f"Failed to save application process: {e}")

    def fill_up(self, job_context: JobContext) -> None:
        job = job_context.job
        logger.debug(f"Filling up form sections for job: {job}")

        input_elements = self.job_application_page.get_input_elements()

        try:
            for element in input_elements:
                self._process_form_element(element, job_context)

        except Exception as e:
            logger.error(
                f"Failed to fill up form sections: {e} {traceback.format_exc()}"
            )

    def _process_form_element(
        self, element: WebElement, job_context: JobContext
    ) -> None:
        logger.debug(f"Processing form element {element}")
        if self.job_application_page.is_upload_field(element):
            self._handle_upload_fields(element, job_context)
        else:
            self._fill_additional_questions(job_context)

    def _handle_dropdown_fields(self, element: WebElement) -> None:
        logger.debug("Handling dropdown fields")

        dropdown = element.find_element(By.TAG_NAME, "select")
        select = Select(dropdown)
        dropdown_id = dropdown.get_attribute("id")
        if "phoneNumber-Country" in dropdown_id:
            country = self.resume_generator_manager.get_resume_country()
            if country:
                try:
                    select.select_by_value(country)
                    logger.debug(f"Selected phone country: {country}")
                    return True
                except NoSuchElementException:
                    logger.warning(f"Country {country} not found in dropdown options")

        options = [option.text for option in select.options]
        logger.debug(f"Dropdown options found: {options}")

        parent_element = dropdown.find_element(By.XPATH, "../..")

        label_elements = parent_element.find_elements(By.TAG_NAME, "label")
        if label_elements:
            question_text = label_elements[0].text.lower()
        else:
            question_text = "unknown"

        logger.debug(f"Detected question text: {question_text}")

        existing_answer = None
        current_question_sanitized = self._sanitize_text(question_text)
        for item in self.all_data:
            if (
                current_question_sanitized in item["question"]
                and item["type"] == "dropdown"
            ):
                existing_answer = item["answer"]
                break

        if existing_answer:
            logger.debug(
                f"Found existing answer for question '{question_text}': {existing_answer}"
            )
        else:
            logger.debug(
                f"No existing answer found, querying model for: {question_text}"
            )
            existing_answer = self.gpt_answerer.answer_question_from_options(
                question_text, options
            )
            logger.debug(f"Model provided answer: {existing_answer}")
            self._save_questions_to_json(
                {
                    "type": "dropdown",
                    "question": question_text,
                    "answer": existing_answer,
                }
            )
            self.all_data = self._load_questions_from_json()

        if existing_answer in options:
            select.select_by_visible_text(existing_answer)
            logger.debug(f"Selected option: {existing_answer}")
            self.job_application.save_application_data(
                {
                    "type": "dropdown",
                    "question": question_text,
                    "answer": existing_answer,
                }
            )
        else:
            logger.error(
                f"Answer '{existing_answer}' is not a valid option in the dropdown"
            )
            raise Exception(f"Invalid option selected: {existing_answer}")

    def _handle_upload_fields(
        self, element: WebElement, job_context: JobContext
    ) -> None:
        logger.debug("Handling upload fields")

        file_upload_elements = self.job_application_page.get_file_upload_elements()

        for element in file_upload_elements:

            file_upload_element_heading = (
                self.job_application_page.get_upload_element_heading(element)
            )

            output = self.gpt_answerer.determine_resume_or_cover(
                file_upload_element_heading
            )

            if "resume" in output:
                logger.debug("Uploading resume")
                if self.resume_path is not None and os.path.isfile(self.resume_path):
                    resume_file_path = os.path.abspath(self.resume_path)
                    self.job_application_page.upload_file(element, resume_file_path)
                    job_context.job.resume_path = resume_file_path
                    job_context.job_application.resume_path = str(resume_file_path)
                    logger.debug(f"Resume uploaded from path: {resume_file_path}")
                else:
                    logger.debug(
                        "Resume path not found or invalid, generating new resume"
                    )
                    self._create_and_upload_resume(element, job_context)

            elif "cover" in output:
                logger.debug("Uploading cover letter")
                self._create_and_upload_cover_letter(element, job_context)

        logger.debug("Finished handling upload fields")

    def _create_and_upload_resume(self, element, job_context: JobContext):
        job = job_context.job
        job_application = job_context.job_application
        logger.debug("Starting the process of creating and uploading resume.")
        folder_path = "generated_cv"

        try:
            if not os.path.exists(folder_path):
                logger.debug(f"Creating directory at path: {folder_path}")
            os.makedirs(folder_path, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create directory: {folder_path}. Error: {e}")
            raise

        while True:
            try:
                timestamp = int(time.time())
                file_path_pdf = os.path.join(folder_path, f"CV_{timestamp}.pdf")
                logger.debug(f"Generated file path for resume: {file_path_pdf}")

                logger.debug(f"Generating resume for job: {job.title} at {job.company}")
                resume_pdf_base64 = self.resume_generator_manager.pdf_base64(
                    job_description_text=job.description
                )
                with open(file_path_pdf, "xb") as f:
                    f.write(base64.b64decode(resume_pdf_base64))
                logger.debug(
                    f"Resume successfully generated and saved to: {file_path_pdf}"
                )

                break
            except HTTPStatusError as e:
                if e.response.status_code == 429:

                    retry_after = e.response.headers.get("retry-after")
                    retry_after_ms = e.response.headers.get("retry-after-ms")

                    if retry_after:
                        wait_time = int(retry_after)
                        logger.warning(
                            f"Rate limit exceeded, waiting {wait_time} seconds before retrying..."
                        )
                    elif retry_after_ms:
                        wait_time = int(retry_after_ms) / 1000.0
                        logger.warning(
                            f"Rate limit exceeded, waiting {wait_time} milliseconds before retrying..."
                        )
                    else:
                        wait_time = 20
                        logger.warning(
                            f"Rate limit exceeded, waiting {wait_time} seconds before retrying..."
                        )

                    time.sleep(wait_time)
                else:
                    logger.error(f"HTTP error: {e}")
                    raise

            except Exception as e:
                logger.error(f"Failed to generate resume: {e}")
                tb_str = traceback.format_exc()
                logger.error(f"Traceback: {tb_str}")
                if "RateLimitError" in str(e):
                    logger.warning("Rate limit error encountered, retrying...")
                    time.sleep(20)
                else:
                    raise

        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Resume file size: {file_size} bytes")
        if file_size > max_file_size:
            logger.error(f"Resume file size exceeds 2 MB: {file_size} bytes")
            raise ValueError("Resume file size exceeds the maximum limit of 2 MB.")

        allowed_extensions = {".pdf", ".doc", ".docx"}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Resume file extension: {file_extension}")
        if file_extension not in allowed_extensions:
            logger.error(f"Invalid resume file format: {file_extension}")
            raise ValueError(
                "Resume file format is not allowed. Only PDF, DOC, and DOCX formats are supported."
            )

        try:
            logger.debug(f"Uploading resume from path: {file_path_pdf}")
            element.send_keys(os.path.abspath(file_path_pdf))
            job.resume_path = os.path.abspath(file_path_pdf)
            job_application.resume_path = os.path.abspath(file_path_pdf)
            time.sleep(2)
            logger.debug(f"Resume created and uploaded successfully: {file_path_pdf}")
        except Exception as e:
            tb_str = traceback.format_exc()
            logger.error(f"Resume upload failed: {tb_str}")
            raise Exception(f"Upload failed: \nTraceback:\n{tb_str}")

    def _create_and_upload_cover_letter(
        self, element: WebElement, job_context: JobContext
    ) -> None:
        job = job_context.job
        logger.debug("Starting the process of creating and uploading cover letter.")

        cover_letter_text = self.gpt_answerer.answer_question_textual_wide_range(
            "Write a cover letter"
        )

        folder_path = "generated_cv"

        try:

            if not os.path.exists(folder_path):
                logger.debug(f"Creating directory at path: {folder_path}")
            os.makedirs(folder_path, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create directory: {folder_path}. Error: {e}")
            raise

        while True:
            try:
                timestamp = int(time.time())
                file_path_pdf = os.path.join(
                    folder_path, f"Cover_Letter_{timestamp}.pdf"
                )
                logger.debug(f"Generated file path for cover letter: {file_path_pdf}")

                c = canvas.Canvas(file_path_pdf, pagesize=A4)
                page_width, page_height = A4
                text_object = c.beginText(50, page_height - 50)
                text_object.setFont("Helvetica", 12)

                max_width = page_width - 100
                bottom_margin = 50
                available_height = page_height - bottom_margin - 50

                def split_text_by_width(text, font, font_size, max_width):
                    wrapped_lines = []
                    for line in text.splitlines():

                        if stringWidth(line, font, font_size) > max_width:
                            words = line.split()
                            new_line = ""
                            for word in words:
                                if (
                                    stringWidth(new_line + word + " ", font, font_size)
                                    <= max_width
                                ):
                                    new_line += word + " "
                                else:
                                    wrapped_lines.append(new_line.strip())
                                    new_line = word + " "
                            wrapped_lines.append(new_line.strip())
                        else:
                            wrapped_lines.append(line)
                    return wrapped_lines

                lines = split_text_by_width(
                    cover_letter_text, "Helvetica", 12, max_width
                )

                for line in lines:
                    text_height = text_object.getY()
                    if text_height > bottom_margin:
                        text_object.textLine(line)
                    else:

                        c.drawText(text_object)
                        c.showPage()
                        text_object = c.beginText(50, page_height - 50)
                        text_object.setFont("Helvetica", 12)
                        text_object.textLine(line)

                c.drawText(text_object)
                c.save()
                logger.debug(
                    f"Cover letter successfully generated and saved to: {file_path_pdf}"
                )

                break
            except Exception as e:
                logger.error(f"Failed to generate cover letter: {e}")
                tb_str = traceback.format_exc()
                logger.error(f"Traceback: {tb_str}")
                raise

        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Cover letter file size: {file_size} bytes")
        if file_size > max_file_size:
            logger.error(f"Cover letter file size exceeds 2 MB: {file_size} bytes")
            raise ValueError(
                "Cover letter file size exceeds the maximum limit of 2 MB."
            )

        allowed_extensions = {".pdf", ".doc", ".docx"}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Cover letter file extension: {file_extension}")
        if file_extension not in allowed_extensions:
            logger.error(f"Invalid cover letter file format: {file_extension}")
            raise ValueError(
                "Cover letter file format is not allowed. Only PDF, DOC, and DOCX formats are supported."
            )

        try:

            logger.debug(f"Uploading cover letter from path: {file_path_pdf}")
            element.send_keys(os.path.abspath(file_path_pdf))
            job.cover_letter_path = os.path.abspath(file_path_pdf)
            job_context.job_application.cover_letter_path = os.path.abspath(
                file_path_pdf
            )
            time.sleep(2)
            logger.debug(
                f"Cover letter created and uploaded successfully: {file_path_pdf}"
            )
        except Exception as e:
            tb_str = traceback.format_exc()
            logger.error(f"Cover letter upload failed: {tb_str}")
            raise Exception(f"Upload failed: \nTraceback:\n{tb_str}")

    def _fill_additional_questions(self, job_context: JobContext) -> None:
        logger.debug("Filling additional questions")
        form_sections = self.job_application_page.get_form_sections()
        for section in form_sections:
            self._process_form_section(job_context, section)

    def _process_form_section(
        self, job_context: JobContext, section: WebElement
    ) -> None:
        logger.debug("Processing form section")
        if self.job_application_page.is_terms_of_service(section):
            logger.debug("Handled terms of service")
            self.job_application_page.accept_terms_of_service(section)
            return

        if self.job_application_page.is_radio_question(section):
            radio_question = self.job_application_page.web_element_to_radio_question(
                section
            )
            self._handle_radio_question(job_context, radio_question, section)
            logger.debug("Handled radio button")
            return

        if self.job_application_page.is_textbox_question(section):
            self._handle_textbox_question(job_context, section)
            logger.debug("Handled textbox question")
            return
        
        if self.job_application_page.is_date_question(section):
            self._handle_date_question(job_context, section)
            logger.debug("Handled date question")
            return

        if self._find_and_handle_dropdown_question(job_context, section):
            logger.debug("Handled dropdown question")
            return

    def _handle_radio_question(
        self,
        job_context: JobContext,
        radio_question: RadioQuestion,
        section: WebElement,
    ) -> None:
        job_application = job_context.job_application

        question_text = radio_question.question
        options = radio_question.options

        existing_answer = None
        current_question_sanitized = self._sanitize_text(question_text)
        for item in self.all_data:
            if (
                current_question_sanitized in item["question"]
                and item["type"] == "radio"
            ):
                existing_answer = item
                break

        if existing_answer:
            self.job_application_page.select_radio_option(
                section, existing_answer["answer"]
            )
            job_application.save_application_data(existing_answer)
            logger.debug("Selected existing radio answer")
            return

        answer = self.gpt_answerer.answer_question_from_options(question_text, options)
        self._save_questions_to_json(
            {"type": "radio", "question": question_text, "answer": answer}
        )
        self.all_data = self._load_questions_from_json()
        job_application.save_application_data(
            {"type": "radio", "question": question_text, "answer": answer}
        )
        self.job_application_page.select_radio_option(section, answer)
        logger.debug("Selected new radio answer")
        return

    def _handle_textbox_question(
        self, job_context: JobContext, section: WebElement
    ) -> None:
 
        textbox_question = self.job_application_page.web_element_to_textbox_question(
            section
        )

        is_cover_letter = textbox_question.is_cover_letter
        question_text = textbox_question.question
        question_type = textbox_question.type.value
        is_numeric = textbox_question.type is TextBoxQuestionType.NUMERIC

        # Look for existing answer if it's not a cover letter field
        existing_answer = None
        if not is_cover_letter:
            current_question_sanitized = self._sanitize_text(question_text)
            for item in self.all_data:
                if (
                    item["question"] == current_question_sanitized
                    and item.get("type") == question_type
                ):
                    existing_answer = item["answer"]
                    logger.debug(f"Found existing answer: {existing_answer}")
                    break

        if existing_answer and not is_cover_letter:
            answer = existing_answer
            logger.debug(f"Using existing answer: {answer}")
        else:
            if is_numeric:
                answer = self.gpt_answerer.answer_question_numeric(question_text)
                logger.debug(f"Generated numeric answer: {answer}")
            else:
                answer = self.gpt_answerer.answer_question_textual_wide_range(
                    question_text
                )
                logger.debug(f"Generated textual answer: {answer}")

        # Save non-cover letter answers
        if not is_cover_letter and not existing_answer:
            self._save_questions_to_json(
                {"type": question_type, "question": question_text, "answer": answer}
            )
            self.all_data = self._load_questions_from_json()
            logger.debug("Saved non-cover letter answer to JSON.")

        self.job_application_page.fill_textbox_question(section, answer)
        logger.debug("Entered answer into the textbox.")

        job_context.job_application.save_application_data(
            {"type": question_type, "question": question_text, "answer": answer}
        )

        return

    def _handle_date_question(
        self, job_context: JobContext, section: WebElement
    ) -> bool:
        job_application = job_context.job_application
        date_fields = section.find_elements(By.CLASS_NAME, "artdeco-datepicker__input ")
        if date_fields:
            date_field = date_fields[0]
            question_text = section.text.lower()
            answer_date = self.gpt_answerer.answer_question_date()
            answer_text = answer_date.strftime("%Y-%m-%d")

            existing_answer = None
            current_question_sanitized = self._sanitize_text(question_text)
            for item in self.all_data:
                if (
                    current_question_sanitized in item["question"]
                    and item["type"] == "date"
                ):
                    existing_answer = item
                    break

            if existing_answer:
                self._enter_text(date_field, existing_answer["answer"])
                logger.debug("Entered existing date answer")
                job_application.save_application_data(existing_answer)
                return True

            self._save_questions_to_json(
                {"type": "date", "question": question_text, "answer": answer_text}
            )
            self.all_data = self._load_questions_from_json()
            job_application.save_application_data(
                {"type": "date", "question": question_text, "answer": answer_text}
            )
            self._enter_text(date_field, answer_text)
            logger.debug("Entered new date answer")
            return True
        return False

    def _find_and_handle_dropdown_question(
        self, job_context: JobContext, section: WebElement
    ) -> bool:
        job_application = job_context.job_application
        try:
            question = section.find_element(
                By.CLASS_NAME, "jobs-easy-apply-form-element"
            )

            dropdowns = question.find_elements(By.TAG_NAME, "select")
            if not dropdowns:
                dropdowns = section.find_elements(
                    By.CSS_SELECTOR, "[data-test-text-entity-list-form-select]"
                )

            if dropdowns:
                dropdown = dropdowns[0]
                select = Select(dropdown)
                options = [option.text for option in select.options]

                logger.debug(f"Dropdown options found: {options}")

                question_text = question.find_element(By.TAG_NAME, "label").text.lower()
                logger.debug(
                    f"Processing dropdown or combobox question: {question_text}"
                )

                current_selection = select.first_selected_option.text
                logger.debug(f"Current selection: {current_selection}")

                existing_answer = None
                current_question_sanitized = self._sanitize_text(question_text)
                for item in self.all_data:
                    if (
                        current_question_sanitized in item["question"]
                        and item["type"] == "dropdown"
                    ):
                        existing_answer = item["answer"]
                        break

                if existing_answer:
                    logger.debug(
                        f"Found existing answer for question '{question_text}': {existing_answer}"
                    )
                    job_application.save_application_data(
                        {
                            "type": "dropdown",
                            "question": question_text,
                            "answer": existing_answer,
                        }
                    )
                    if current_selection != existing_answer:
                        logger.debug(f"Updating selection to: {existing_answer}")
                        self._select_dropdown_option(dropdown, existing_answer)
                else:
                    logger.debug(
                        f"No existing answer found, querying model for: {question_text}"
                    )
                    answer = self.gpt_answerer.answer_question_from_options(
                        question_text, options
                    )
                    self._save_questions_to_json(
                        {
                            "type": "dropdown",
                            "question": question_text,
                            "answer": answer,
                        }
                    )
                    self.all_data = self._load_questions_from_json()
                    job_application.save_application_data(
                        {
                            "type": "dropdown",
                            "question": question_text,
                            "answer": answer,
                        }
                    )
                    self._select_dropdown_option(dropdown, answer)
                    logger.debug(f"Selected new dropdown answer: {answer}")

                return True

            else:

                logger.debug(f"No dropdown found. Logging elements for debugging.")
                elements = section.find_elements(By.XPATH, ".//*")
                logger.debug(
                    f"Elements found: {[element.tag_name for element in elements]}"
                )
                return False

        except Exception as e:
            logger.warning(
                f"Failed to handle dropdown or combobox question: {e}", exc_info=True
            )
            return False

    def _select_dropdown_option(self, element: WebElement, text: str) -> None:
        logger.debug(f"Selecting dropdown option: {text}")
        select = Select(element)
        select.select_by_visible_text(text)

    def _save_questions_to_json(self, question_data: dict) -> None:
        output_file = "answers.json"
        question_data["question"] = self._sanitize_text(question_data["question"])

        logger.debug(f"Checking if question data already exists: {question_data}")
        try:
            with open(output_file, "r+") as f:
                try:
                    data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError(
                            "JSON file format is incorrect. Expected a list of questions."
                        )
                except json.JSONDecodeError:
                    logger.error("JSON decoding failed")
                    data = []

                should_be_saved: bool = not question_already_exists_in_data(
                    question_data["question"], data
                ) and not self.answer_contians_company_name(question_data["answer"])

                if should_be_saved:
                    logger.debug("New question found, appending to JSON")
                    data.append(question_data)
                    f.seek(0)
                    json.dump(data, f, indent=4)
                    f.truncate()
                    logger.debug("Question data saved successfully to JSON")
                else:
                    logger.debug("Question already exists, skipping save")
        except FileNotFoundError:
            logger.warning("JSON file not found, creating new file")
            with open(output_file, "w") as f:
                json.dump([question_data], f, indent=4)
            logger.debug("Question data saved successfully to new JSON file")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Error saving questions data to JSON file: {tb_str}")
            raise Exception(
                f"Error saving questions data to JSON file: \nTraceback:\n{tb_str}"
            )

    def _sanitize_text(self, text: str) -> str:
        sanitized_text = text.lower().strip().replace('"', "").replace("\\", "")
        sanitized_text = (
            re.sub(r"[\x00-\x1F\x7F]", "", sanitized_text)
            .replace("\n", " ")
            .replace("\r", "")
            .rstrip(",")
        )
        logger.debug(f"Sanitized text: {sanitized_text}")
        return sanitized_text

    def _find_existing_answer(self, question_text):
        for item in self.all_data:
            if self._sanitize_text(item["question"]) == self._sanitize_text(
                question_text
            ):
                return item
        return None

    def answer_contians_company_name(self, answer: Any) -> bool:
        return (
            isinstance(answer, str)
            and not self.current_job.company is None
            and self.current_job.company in answer
        )
