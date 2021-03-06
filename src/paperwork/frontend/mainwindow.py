#    Paperwork - Using OCR to grep dead trees the easy way
#    Copyright (C) 2012  Jerome Flesch
#    Copyright (C) 2012  Sebastien Maccagnoni-Munch
#
#    Paperwork is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Paperwork is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Paperwork.  If not, see <http://www.gnu.org/licenses/>.

from copy import copy
import os
import sys
import threading
import time

import PIL.Image
import gettext
import logging
import cairo
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GObject
from gi.repository import GdkPixbuf

import pyinsane.rawapi

from paperwork.frontend.aboutdialog import AboutDialog
from paperwork.frontend.actions import SimpleAction
from paperwork.frontend.doceditdialog import DocEditDialog
from paperwork.frontend.label_editor import LabelEditor
from paperwork.frontend.multiscan import MultiscanDialog
from paperwork.frontend.page_edit import PageEditingDialog
from paperwork.frontend.settingswindow import SettingsWindow
from paperwork.frontend.workers import IndependentWorker
from paperwork.frontend.workers import Worker
from paperwork.frontend.workers import WorkerProgressUpdater
from paperwork.backend import docimport
from paperwork.backend.common.page import DummyPage
from paperwork.backend.docsearch import DocSearch
from paperwork.backend.docsearch import DummyDocSearch
from paperwork.backend.img.doc import ImgDoc
from paperwork.backend.img.page import ImgPage
from paperwork.util import add_img_border
from paperwork.util import ask_confirmation
from paperwork.util import image2pixbuf
from paperwork.util import load_uifile
from paperwork.util import popup_no_scanner_found
from paperwork.util import sizeof_fmt

_ = gettext.gettext
logger = logging.getLogger(__name__)


def check_workdir(config):
    """
    Check that the current work dir (see config.PaperworkConfig) exists. If
    not, open the settings dialog.
    """
    try:
        os.stat(config.workdir)
        return
    except OSError, exc:
        logger.error("Unable to stat dir '%s': %s --> mkdir"
               % (config.workdir, exc))

    os.mkdir(config.workdir, 0750)


def check_scanner(main_win, config):
    if config.scanner_devid is not None:
        return True
    main_win.actions['open_settings'][1].do()
    return False


def sort_documents_by_date(documents):
    documents.sort()
    documents.reverse()


class WorkerDocIndexLoader(Worker):
    """
    Reload the doc index
    """

    __gsignals__ = {
        'index-loading-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'index-loading-progression': (GObject.SignalFlags.RUN_LAST, None,
                                      (GObject.TYPE_FLOAT,
                                       GObject.TYPE_STRING)),
        'index-loading-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = True

    def __init__(self, main_window, config):
        Worker.__init__(self, "Document reindexation")
        self.__main_win = main_window
        self.__config = config

    def __progress_cb(self, progression, total, step, doc=None):
        """
        Update the main progress bar
        """
        if progression % 50 != 0:
            return
        txt = None
        if step == DocSearch.INDEX_STEP_LOADING:
            txt = _('Loading ...')
        elif step == DocSearch.INDEX_STEP_CLEANING:
            txt = _('Cleaning ...')
        else:
            assert()  # unknown progression type
            txt = ""
        if doc is not None:
            txt += (" (%s)" % (doc.name))
        self.emit('index-loading-progression', float(progression) / total, txt)
        if not self.can_run:
            raise StopIteration()

    def do(self):
        self.emit('index-loading-start')
        try:
            docsearch = DocSearch(self.__config.workdir, self.__progress_cb)
            self.__main_win.docsearch = docsearch
        except StopIteration:
            logger.error("Indexation interrupted")
        self.emit('index-loading-end')


GObject.type_register(WorkerDocIndexLoader)


class WorkerDocExaminer(IndependentWorker):
    """
    Look for modified documents
    """

    __gsignals__ = {
        'doc-examination-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'doc-examination-progression': (GObject.SignalFlags.RUN_LAST, None,
                                        (GObject.TYPE_FLOAT,
                                         GObject.TYPE_STRING)),
        'doc-examination-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = True

    def __init__(self, main_window, config):
        IndependentWorker.__init__(self, "Document examination")
        self.__main_win = main_window
        self.__config = config

    def __progress_cb(self, progression, total, step, doc=None):
        """
        Update the main progress bar
        """
        if progression % 10 != 0:
            return
        txt = None
        if step == DocSearch.INDEX_STEP_CHECKING:
            txt = _('Checking ...')
        else:
            assert()  # unknown progression type
            txt = ""
        if doc is not None:
            txt += (" (%s)" % (str(doc)))
        self.emit('doc-examination-progression',
                  float(progression) / total, txt)
        if not self.can_run:
            raise StopIteration()

    def do(self):
        self.emit('doc-examination-start')
        self.new_docs = set()  # documents
        self.docs_changed = set()  # documents
        self.docs_missing = set()  # document ids
        try:
            doc_examiner = self.__main_win.docsearch.get_doc_examiner()
            doc_examiner.examine_rootdir(
                self.__on_new_doc,
                self.__on_doc_changed,
                self.__on_doc_missing,
                self.__progress_cb)
        except StopIteration:
            logger.error("Document examination interrupted")
        finally:
            self.emit('doc-examination-end')

    def __on_new_doc(self, doc):
        self.new_docs.add(doc)

    def __on_doc_changed(self, doc):
        self.docs_changed.add(doc)

    def __on_doc_missing(self, docid):
        self.docs_missing.add(docid)


GObject.type_register(WorkerDocExaminer)


class WorkerIndexUpdater(Worker):
    """
    Look for modified documents
    """

    __gsignals__ = {
        'index-update-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'index-update-progression': (GObject.SignalFlags.RUN_LAST, None,
                                     (GObject.TYPE_FLOAT,
                                      GObject.TYPE_STRING)),
        'index-update-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = True

    def __init__(self, main_window, config):
        Worker.__init__(self, "Document index update")
        self.__main_win = main_window
        self.__config = config

    def do(self, new_docs=[], upd_docs=[], del_docs=[], optimize=True):
        self.emit('index-update-start')
        try:
            index_updater = self.__main_win.docsearch.get_index_updater(
                optimize=optimize)

            docs = [
                (_("Indexing new document ..."), new_docs,
                 index_updater.add_doc),
                (_("Reindexing modified document ..."), upd_docs,
                 index_updater.upd_doc),
                (_("Removing deleted document from index ..."), del_docs,
                 index_updater.del_doc),
            ]

            progression = float(0)
            total = len(new_docs) + len(upd_docs) + len(del_docs)

            for (op_name, doc_bunch, op) in docs:
                for doc in doc_bunch:
                    self.emit('index-update-progression',
                              (progression * 0.75) / total,
                              "%s (%s)" % (op_name, str(doc)))
                    op(doc)
                    progression += 1
                    if not self.can_run:
                        index_updater.cancel()
                        self.emit('index-update-end')

            self.emit('index-update-progression', 0.75,
                      _("Writing index ..."))
            index_updater.commit()
            self.emit('index-update-progression', 1.0, "")
        finally:
            self.emit('index-update-end')


GObject.type_register(WorkerIndexUpdater)


class WorkerDocSearcher(Worker):
    """
    Search the documents
    """

    __gsignals__ = {
        'search-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        # first obj: array of documents
        # second obj: array of suggestions
        'search-result': (GObject.SignalFlags.RUN_LAST, None,
                          (GObject.TYPE_PYOBJECT, GObject.TYPE_PYOBJECT)),
    }

    can_interrupt = True

    def __init__(self, main_window, config):
        Worker.__init__(self, "Search")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        for t in range(0, 10):
            if not self.can_run or self.paused:
                return
            time.sleep(0.05)

        sentence = unicode(self.__main_win.search_field.get_text(),
                           encoding='utf-8')

        self.emit('search-start')
        if not self.can_run:
            return

        documents = self.__main_win.docsearch.find_documents(sentence)
        if not self.can_run:
            return

        if sentence == u"":
            # when no specific search has been done, the sorting is always
            # the same
            sort_documents_by_date(documents)
            # append a new document to the list
            documents.insert(0, ImgDoc(self.__config.workdir))
        else:
            for (widget, sort_func) in self.__main_win.sortings:
                if widget.get_active():
                    sort_func(documents)
                    break
        if not self.can_run:
            return

        suggestions = self.__main_win.docsearch.find_suggestions(sentence)
        if not self.can_run:
            return

        self.emit('search-result', documents, suggestions)


GObject.type_register(WorkerDocSearcher)


class WorkerPageThumbnailer(Worker):
    """
    Generate page thumbnails
    """

    __gsignals__ = {
        'page-thumbnailing-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'page-thumbnailing-page-done': (GObject.SignalFlags.RUN_LAST, None,
                                        (GObject.TYPE_INT,
                                         GObject.TYPE_PYOBJECT)),
        'page-thumbnailing-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = True

    def __init__(self, main_window):
        Worker.__init__(self, "Page thumbnailing")
        self.__main_win = main_window

    def do(self):
        search = unicode(self.__main_win.search_field.get_text(),
                         encoding='utf-8')

        self.emit('page-thumbnailing-start')
        for page_idx in range(0, self.__main_win.doc.nb_pages):
            page = self.__main_win.doc.pages[page_idx]
            img = page.get_thumbnail(WorkerDocThumbnailer.THUMB_WIDTH)
            img = img.copy()
            if search != u"" and search in page:
                img = add_img_border(img, color="#009e00", width=3)
            else:
                img = add_img_border(img)
            pixbuf = image2pixbuf(img)
            if not self.can_run:
                self.emit('page-thumbnailing-end')
                return
            self.emit('page-thumbnailing-page-done', page_idx, pixbuf)
        self.emit('page-thumbnailing-end')


GObject.type_register(WorkerPageThumbnailer)


class WorkerDocThumbnailer(Worker):
    """
    Generate doc list thumbnails
    """

    THUMB_WIDTH = 150
    THUMB_HEIGHT = 220

    __gsignals__ = {
        'doc-thumbnailing-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'doc-thumbnailing-doc-done': (GObject.SignalFlags.RUN_LAST, None,
                                      (GObject.TYPE_INT,
                                       GObject.TYPE_PYOBJECT)),
        'doc-thumbnailing-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = True
    can_pause = True

    def __init__(self, main_window):
        Worker.__init__(self, "Doc thumbnailing")
        self.__main_win = main_window

    def do(self, doc_indexes=None, resume=0):
        for t in range(0, 10):
            if not self.can_run or self.paused:
                return resume
            time.sleep(0.05)

        self.emit('doc-thumbnailing-start')

        doclist = self.__main_win.lists['matches']['doclist']
        if doc_indexes is None:
            if resume >= len(doclist):
                resume = 0
            doc_indexes = range(resume, len(doclist))
        else:
            if resume >= len(doc_indexes):
                resume = 0
            doc_indexes = doc_indexes[resume:]

        for doc_idx in doc_indexes:
            if self.paused:
                return resume
            if not self.can_run:
                self.emit('doc-thumbnailing-end')
                return resume
            doc = doclist[doc_idx]
            if doc.nb_pages <= 0:
                resume += 1
                continue
            img = doc.pages[0].get_thumbnail(self.THUMB_WIDTH)

            (width, height) = img.size
            # always make sure the thumbnail has a specific height
            # otherwise the scrollbar keep moving while loading
            if height > self.THUMB_HEIGHT:
                img = img.crop((0, 0, width, self.THUMB_HEIGHT))
                img = img.copy()
            else:
                new_img = PIL.Image.new('RGBA', (width, self.THUMB_HEIGHT),
                                    '#FFFFFF')
                h = (self.THUMB_HEIGHT - height) / 2
                new_img.paste(img, (0, h, width, h+height))
                img = new_img

            img = add_img_border(img)
            pixbuf = image2pixbuf(img)
            self.emit('doc-thumbnailing-doc-done', doc_idx, pixbuf)
            resume += 1
        self.emit('doc-thumbnailing-end')
        return 0


GObject.type_register(WorkerDocThumbnailer)


class WorkerImgBuilder(Worker):
    """
    Resize and paint on the page
    """
    __gsignals__ = {
        'img-building-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'img-building-result-pixbuf': (GObject.SignalFlags.RUN_LAST, None,
                                       (GObject.TYPE_FLOAT, GObject.TYPE_INT,
                                        GObject.TYPE_PYOBJECT,  # pixbuf
                                        # array of boxes
                                        GObject.TYPE_PYOBJECT,
                                        )),
        'img-building-result-clear': (GObject.SignalFlags.RUN_LAST, None, ()),
        'img-building-result-stock': (GObject.SignalFlags.RUN_LAST, None,
                                      (GObject.TYPE_STRING, )),
    }

    # even if it's not true, this process is not really long, so it doesn't
    # really matter
    can_interrupt = True

    def __init__(self, main_window):
        Worker.__init__(self, "Building page image")
        self.__main_win = main_window

    def do(self):
        self.emit('img-building-start')

        if self.__main_win.page.img is None:
            self.emit('img-building-result-clear')
            return

        # to keep the GUI smooth
        for t in range(0, 25):
            if not self.can_run:
                break
            time.sleep(0.01)
        if not self.can_run:
            self.emit('img-building-result-clear')
            return

        try:
            img = self.__main_win.page.img

            pixbuf = image2pixbuf(img)
            original_width = pixbuf.get_width()

            factor = self.__main_win.get_zoom_factor(original_width)
            logger.info("Zoom: %f" % (factor))

            wanted_width = int(factor * pixbuf.get_width())
            wanted_height = int(factor * pixbuf.get_height())
            pixbuf = pixbuf.scale_simple(wanted_width, wanted_height,
                                         GdkPixbuf.InterpType.BILINEAR)

            self.emit('img-building-result-pixbuf', factor, original_width,
                      pixbuf, self.__main_win.page.boxes)
        except Exception, exc:
            self.emit('img-building-result-stock', Gtk.STOCK_DIALOG_ERROR)
            raise exc


GObject.type_register(WorkerImgBuilder)


class WorkerLabelUpdater(Worker):
    """
    Resize and paint on the page
    """
    __gsignals__ = {
        'label-updating-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'label-updating-doc-updated': (GObject.SignalFlags.RUN_LAST, None,
                                       (GObject.TYPE_FLOAT,
                                        GObject.TYPE_STRING)),
        'label-updating-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = False

    def __init__(self, main_window):
        Worker.__init__(self, "Updating label")
        self.__main_win = main_window

    def __progress_cb(self, progression, total, step, doc):
        self.emit('label-updating-doc-updated', float(progression) / total,
                  doc.name)

    def do(self, old_label, new_label):
        self.emit('label-updating-start')
        try:
            self.__main_win.docsearch.update_label(old_label, new_label,
                                                   self.__progress_cb)
        finally:
            self.emit('label-updating-end')


GObject.type_register(WorkerLabelUpdater)


class WorkerLabelDeleter(Worker):
    """
    Resize and paint on the page
    """
    __gsignals__ = {
        'label-deletion-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'label-deletion-doc-updated': (GObject.SignalFlags.RUN_LAST, None,
                                       (GObject.TYPE_FLOAT,
                                        GObject.TYPE_STRING)),
        'label-deletion-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = False

    def __init__(self, main_window):
        Worker.__init__(self, "Removing label")
        self.__main_win = main_window

    def __progress_cb(self, progression, total, step, doc):
        self.emit('label-deletion-doc-updated', float(progression) / total,
                  doc.name)

    def do(self, label):
        self.emit('label-deletion-start')
        try:
            self.__main_win.docsearch.destroy_label(label, self.__progress_cb)
        finally:
            self.emit('label-deletion-end')


GObject.type_register(WorkerLabelDeleter)


class WorkerOCRRedoer(Worker):
    """
    Resize and paint on the page
    """
    __gsignals__ = {
        'redo-ocr-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'redo-ocr-doc-updated': (GObject.SignalFlags.RUN_LAST, None,
                                 (GObject.TYPE_FLOAT, GObject.TYPE_STRING)),
        'redo-ocr-end': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    can_interrupt = False

    def __init__(self, main_window, config):
        Worker.__init__(self, "Redoing OCR")
        self.__main_win = main_window
        self.__config = config

    def __progress_cb(self, progression, total, step, doc):
        self.emit('redo-ocr-doc-updated', float(progression) / total,
                  doc.name)

    def do(self, doc_target):
        self.emit('redo-ocr-start')
        try:
            doc_target.redo_ocr(self.__config.langs, self.__progress_cb)
        finally:
            self.emit('redo-ocr-end')


GObject.type_register(WorkerOCRRedoer)


class WorkerSingleScan(Worker):
    __gsignals__ = {
        'single-scan-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'single-scan-ocr': (GObject.SignalFlags.RUN_LAST, None, ()),
        'single-scan-done': (GObject.SignalFlags.RUN_LAST, None,
                             (GObject.TYPE_PYOBJECT,)),  # ImgPage
    }

    can_interrupt = True

    def __init__(self, main_window, config):
        Worker.__init__(self, "Scanning page")
        self.__main_win = main_window
        self.__config = config
        self.__ocr_running = False

    def __scan_progress_cb(self, progression, total, step, doc=None):
        if not self.can_run:
            raise Exception("Interrupted by the user")
        if (step == ImgPage.SCAN_STEP_OCR) and (not self.__ocr_running):
            self.emit('single-scan-ocr')
            self.__ocr_running = True

    def do(self, doc):
        self.emit('single-scan-start')

        self.__ocr_running = False
        try:
            scanner = self.__config.get_scanner_inst()
            try:
                scanner.options['source'].value = "Auto"
            except (KeyError, pyinsane.rawapi.SaneException), exc:
                logger.error("Warning: Unable to set scanner source "
                       "to 'Auto': %s" % exc)
            scan_src = scanner.scan(multiple=False)
        except pyinsane.rawapi.SaneException, exc:
            logger.error("No scanner found !")
            GObject.idle_add(popup_no_scanner_found, self.__main_win.window)
            self.emit('single-scan-done', None)
            raise
        doc.scan_single_page(scan_src, scanner.options['resolution'].value,
                             self.__config.scanner_calibration,
                             self.__config.langs,
                             self.__scan_progress_cb)
        page = doc.pages[doc.nb_pages - 1]
        self.__main_win.docsearch.index_page(page)

        self.emit('single-scan-done', page)


GObject.type_register(WorkerSingleScan)


class WorkerImporter(Worker):
    __gsignals__ = {
        'import-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'import-done': (GObject.SignalFlags.RUN_LAST, None,
                        (GObject.TYPE_PYOBJECT,  # Doc
                         GObject.TYPE_PYOBJECT),),  # Page
    }

    can_interrupt = True

    def __init__(self, main_window, config):
        Worker.__init__(self, "Importing file")
        self.__main_win = main_window
        self.__config = config

    def do(self, importer, file_uri):
        self.emit('import-start')
        (doc, page) = importer.import_doc(file_uri, self.__config,
                                          self.__main_win.docsearch,
                                          self.__main_win.doc)
        self.emit('import-done', doc, page)


GObject.type_register(WorkerImporter)


class WorkerExportPreviewer(Worker):
    __gsignals__ = {
        'export-preview-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'export-preview-done': (GObject.SignalFlags.RUN_LAST, None,
                                (GObject.TYPE_INT, GObject.TYPE_PYOBJECT,)),
    }

    can_interrupt = True

    def __init__(self, main_window):
        Worker.__init__(self, "Export previewer")
        self.__main_win = main_window

    def do(self):
        exporter = self.__main_win.export['exporter']
        for i in range(0, 7):
            time.sleep(0.1)
            if not self.can_run:
                return
        self.emit('export-preview-start')
        size = exporter.estimate_size()
        img = exporter.get_img()
        pixbuf = image2pixbuf(img)
        self.emit('export-preview-done', size, pixbuf)


GObject.type_register(WorkerExportPreviewer)


class WorkerPageEditor(Worker):
    __gsignals__ = {
        'page-editing-img-edit': (GObject.SignalFlags.RUN_LAST, None,
                                  (GObject.TYPE_PYOBJECT, )),
        'page-editing-ocr': (GObject.SignalFlags.RUN_LAST, None,
                             (GObject.TYPE_PYOBJECT, )),
        'page-editing-index-upd': (GObject.SignalFlags.RUN_LAST, None,
                                   (GObject.TYPE_PYOBJECT, )),
        'page-editing-done': (GObject.SignalFlags.RUN_LAST, None,
                              (GObject.TYPE_PYOBJECT, )),
    }

    def __init__(self, main_win, config):
        Worker.__init__(self, "Page editor")
        self.__main_win = main_win
        self.__config = config

    def do(self, page, changes=[]):
        self.emit('page-editing-img-edit', page)
        try:
            img = page.img
            for change in changes:
                img = change.do(img, 1.0)
            page.img = img
            self.emit('page-editing-ocr', page)
            page.redo_ocr(self.__config.langs)
            self.emit('page-editing-index-upd', page)
            docsearch = self.__main_win.docsearch
            index_upd = docsearch.get_index_updater(optimize=False)
            index_upd.upd_doc(page.doc)
            index_upd.commit()
        finally:
            self.emit('page-editing-done', page)


GObject.type_register(WorkerPageEditor)


class ActionNewDocument(SimpleAction):
    """
    Starts a new document.
    """
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "New document")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)

        must_insert_new = False

        doclist = self.__main_win.lists['matches']['doclist']
        if (len(doclist) <= 0):
            must_insert_new = True
        else:
            must_insert_new = not doclist[0].is_new

        if must_insert_new:
            doc = ImgDoc(self.__config.workdir)
            doclist.insert(0, doc)
            self.__main_win.lists['matches']['model'].insert(
                0,
                [
                    doc.name,
                    doc,
                    None,
                    None,
                    Gtk.IconSize.DIALOG,
                ])

        path = Gtk.TreePath(0)
        self.__main_win.lists['matches']['gui'].select_path(path)
        self.__main_win.lists['matches']['gui'].scroll_to_path(
            path, False, 0.0, 0.0)


class ActionOpenSelectedDocument(SimpleAction):
    """
    Starts a new document.
    """
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Open selected document")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)

        match_list = self.__main_win.lists['matches']['gui']
        selection_path = match_list.get_selected_items()
        if len(selection_path) <= 0:
            logger.warn("No document selected. Can't open")
            return
        doc_idx = selection_path[0].get_indices()[0]
        doc = self.__main_win.lists['matches']['model'][doc_idx][1]

        logger.info("Showing doc %s" % doc)
        self.__main_win.show_doc(doc)


class ActionStartSimpleWorker(SimpleAction):
    """
    Start a threaded job
    """
    def __init__(self, worker):
        SimpleAction.__init__(self, str(worker))
        self.__worker = worker

    def do(self):
        SimpleAction.do(self)
        self.__worker.start()


class ActionStartSearch(SimpleAction):
    """
    Let the user type keywords to do a document search
    """
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Focus on search field")
        self.__main_win = main_window

    def do(self):
        self.__main_win.search_field.grab_focus()


class ActionUpdateSearchResults(SimpleAction):
    """
    Update search results
    """
    def __init__(self, main_window, refresh_pages=True):
        SimpleAction.__init__(self, "Update search results")
        self.__main_win = main_window
        self.__refresh_pages = refresh_pages

    def do(self):
        SimpleAction.do(self)
        self.__main_win.refresh_doc_list()
        if self.__refresh_pages:
            self.__main_win.refresh_page()

    def on_icon_press_cb(self, entry, iconpos=Gtk.EntryIconPosition.SECONDARY,
                         event=None):
        if iconpos == Gtk.EntryIconPosition.PRIMARY:
            entry.grab_focus()
        else:
            entry.set_text("")


class ActionOpenPageSelected(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self,
                              "Show a page (selected from the page"
                              " thumbnail list)")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        gui_list = self.__main_win.lists['pages']['gui']
        selection_path = gui_list.get_selected_items()
        if len(selection_path) <= 0:
            self.__main_win.show_page(DummyPage(self.__main_win.doc))
            return
        # TODO(Jflesch): We should get the page number from the list content,
        # not from the position of the element in the list
        page_idx = selection_path[0].get_indices()[0]
        page = self.__main_win.doc.pages[page_idx]
        self.__main_win.show_page(page)


class ActionMovePageIndex(SimpleAction):
    def __init__(self, main_window, relative=True, value=0):
        if relative:
            txt = "previous"
            if value > 0:
                txt = "next"
        else:
            if value < 0:
                txt = "last"
            else:
                txt = "page %d" % (value)
        SimpleAction.__init__(self, ("Show the %s page" % (txt)))
        self.relative = relative
        self.value = value
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        page_idx = self.__main_win.page.page_nb
        if self.relative:
            page_idx += self.value
        elif self.value < 0:
            page_idx = self.__main_win.doc.nb_pages - 1
        else:
            page_idx = self.value
        if page_idx < 0 or page_idx >= self.__main_win.doc.nb_pages:
            return
        page = self.__main_win.doc.pages[page_idx]
        self.__main_win.show_page(page)


class ActionOpenPageNb(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Show a page (selected on its number)")
        self.__main_win = main_window

    def entry_changed(self, entry):
        pass

    def do(self):
        SimpleAction.do(self)
        page_nb = self.__main_win.indicators['current_page'].get_text()
        page_nb = int(page_nb) - 1
        if page_nb < 0 or page_nb > self.__main_win.doc.nb_pages:
            return
        page = self.__main_win.doc.pages[page_nb]
        self.__main_win.show_page(page)


class ActionRebuildPage(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Reload current page")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        self.__main_win.workers['img_builder'].stop()
        self.__main_win.workers['img_builder'].start()


class ActionRefreshPage(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Refresh current page")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        self.__main_win.refresh_page()


class ActionLabelSelected(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Label selected")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        for widget in self.__main_win.need_label_widgets:
            widget.set_sensitive(True)
        return True


class ActionToggleLabel(object):
    def __init__(self, main_window):
        self.__main_win = main_window

    def toggle_cb(self, renderer, objpath):
        label = self.__main_win.lists['labels']['model'][objpath][2]
        if not label in self.__main_win.doc.labels:
            logger.info("Action: Adding label '%s' on document '%s'"
                   % (str(label), str(self.__main_win.doc)))
            self.__main_win.docsearch.add_label(self.__main_win.doc, label)
        else:
            logger.info("Action: Removing label '%s' on document '%s'"
                   % (label, self.__main_win.doc))
            self.__main_win.docsearch.remove_label(self.__main_win.doc, label)
        self.__main_win.refresh_label_list()
        self.__main_win.refresh_docs([self.__main_win.doc])

    def connect(self, cellrenderers):
        for cellrenderer in cellrenderers:
            cellrenderer.connect('toggled', self.toggle_cb)


class ActionCreateLabel(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Creating label")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        labeleditor = LabelEditor()
        if labeleditor.edit(self.__main_win.window):
            logger.info("Adding label %s to doc %s" % (labeleditor.label,
                                                 self.__main_win.doc))
            self.__main_win.docsearch.add_label(self.__main_win.doc,
                                                labeleditor.label)
        self.__main_win.refresh_label_list()
        self.__main_win.refresh_docs([self.__main_win.doc])


class ActionEditLabel(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Editing label")
        self.__main_win = main_window

    def do(self):
        if self.__main_win.workers['label_updater'].is_running:
            return

        SimpleAction.do(self)

        label_list = self.__main_win.lists['labels']['gui']
        selection_path = label_list.get_selection().get_selected()
        if selection_path[1] is None:
            logger.warn("No label selected")
            return True
        label = selection_path[0].get_value(selection_path[1], 2)

        new_label = copy(label)
        editor = LabelEditor(new_label)
        if not editor.edit(self.__main_win.window):
            logger.warn("Label edition cancelled")
            return
        logger.info("Label edited. Applying changes")
        if self.__main_win.workers['label_updater'].is_running:
            return
        self.__main_win.workers['label_updater'].start(old_label=label,
                                                       new_label=new_label)


class ActionDeleteLabel(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Deleting label")
        self.__main_win = main_window

    def do(self):
        if self.__main_win.workers['label_deleter'].is_running:
            return

        SimpleAction.do(self)

        if not ask_confirmation(self.__main_win.window):
            return

        label_list = self.__main_win.lists['labels']['gui']
        selection_path = label_list.get_selection().get_selected()
        if selection_path[1] is None:
            logger.warn("No label selected")
            return True
        label = selection_path[0].get_value(selection_path[1], 2)

        if self.__main_win.workers['label_deleter'].is_running:
            return
        self.__main_win.workers['label_deleter'].start(label=label)


class ActionOpenDocDir(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Open doc dir")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        os.system('xdg-open "%s"' % (self.__main_win.doc.path))


class ActionPrintDoc(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Open print dialog")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)

        print_settings = Gtk.PrintSettings()
        print_op = Gtk.PrintOperation()
        print_op.set_print_settings(print_settings)
        print_op.set_n_pages(self.__main_win.doc.nb_pages)
        print_op.set_current_page(self.__main_win.page.page_nb)
        print_op.set_use_full_page(False)
        print_op.set_job_name(str(self.__main_win.doc))
        print_op.set_export_filename(str(self.__main_win.doc) + ".pdf")
        print_op.set_allow_async(True)
        print_op.connect("draw-page", self.__main_win.doc.print_page_cb)
        print_op.set_embed_page_setup(True)
        print_op.run(Gtk.PrintOperationAction.PRINT_DIALOG,
                     self.__main_win.window)


class ActionOpenSettings(SimpleAction):
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Open settings dialog")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)
        sw = SettingsWindow(self.__main_win.window, self.__config)
        sw.connect("need-reindex", self.__reindex_cb)

    def __reindex_cb(self, settings_window):
        self.__main_win.actions['reindex'][1].do()


class ActionSingleScan(SimpleAction):
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Scan a single page")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)
        check_workdir(self.__config)
        if not check_scanner(self.__main_win, self.__config):
            return
        doc = self.__main_win.doc
        self.__main_win.workers['single_scan'].start(doc=doc)


class ActionMultiScan(SimpleAction):
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Scan multiples pages")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)
        check_workdir(self.__config)
        if not check_scanner(self.__main_win, self.__config):
            return
        ms = MultiscanDialog(self.__main_win, self.__config)
        ms.connect("need-show-page",
                   lambda ms_dialog, page:
                   GObject.idle_add(self.__show_page, page))

    def __show_page(self, page):
        self.__main_win.refresh_doc_list()
        self.__main_win.refresh_page_list()
        self.__main_win.show_page(page)


class ActionImport(SimpleAction):
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Import file(s)")
        self.__main_win = main_window
        self.__config = config

    def __select_file(self):
        widget_tree = load_uifile("import.glade")
        dialog = widget_tree.get_object("filechooserdialog")
        dialog.set_local_only(False)
        dialog.set_select_multiple(False)

        response = dialog.run()
        if response != 0:
            logger.info("Import: Canceled by user")
            dialog.destroy()
            return None
        file_uri = dialog.get_uri()
        dialog.destroy()
        logger.info("Import: %s" % file_uri)
        return file_uri

    def __select_importer(self, importers):
        widget_tree = load_uifile("import_select.glade")
        combobox = widget_tree.get_object("comboboxImportAction")
        importer_list = widget_tree.get_object("liststoreImportAction")
        dialog = widget_tree.get_object("dialogImportSelect")

        importer_list.clear()
        for importer in importers:
            importer_list.append([str(importer), importer])

        response = dialog.run()
        if not response:
            raise Exception("Import cancelled by user")

        active_idx = combobox.get_active()
        return import_list[active_idx][1]

    def do(self):
        SimpleAction.do(self)

        check_workdir(self.__config)

        file_uri = self.__select_file()
        if file_uri is None:
            return

        importers = docimport.get_possible_importers(file_uri,
                                                     self.__main_win.doc)
        if len(importers) <= 0:
            msg = (_("Don't know how to import '%s'. Sorry.") %
                   (os.path.basename(file_uri)))
            flags = (Gtk.DialogFlags.MODAL
                     | Gtk.DialogFlags.DESTROY_WITH_PARENT)
            dialog = Gtk.MessageDialog(parent=self.__main_win.window,
                                       flags=flags,
                                       type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.OK,
                                       message_format=msg)
            dialog.run()
            dialog.destroy()
            return

        if len(importers) > 1:
            importer = self.__select_importers(importers)
        else:
            importer = importers[0]

        Gtk.RecentManager().add_item(file_uri)

        self.__main_win.workers['importer'].start(
            importer=importer, file_uri=file_uri)


class ActionDeleteDoc(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Delete document")
        self.__main_win = main_window

    def do(self):
        """
        Ask for confirmation and then delete the document being viewed.
        """
        if not ask_confirmation(self.__main_win.window):
            return
        SimpleAction.do(self)
        logger.info("Deleting ...")
        self.__main_win.doc.destroy()
        logger.info("Deleted")
        self.__main_win.actions['new_doc'][1].do()
        self.__main_win.actions['reindex'][1].do()


class ActionDeletePage(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Delete page")
        self.__main_win = main_window

    def do(self):
        """
        Ask for confirmation and then delete the page being viewed.
        """
        if not ask_confirmation(self.__main_win.window):
            return
        SimpleAction.do(self)
        logger.info("Deleting ...")
        self.__main_win.page.destroy()
        logger.info("Deleted")
        self.__main_win.page = None
        for widget in self.__main_win.need_page_widgets:
            widget.set_sensitive(False)
        self.__main_win.refresh_docs([self.__main_win.doc])
        self.__main_win.refresh_page_list()
        self.__main_win.refresh_label_list()


class ActionRedoDocOCR(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Redoing doc ocr")
        self.__main_win = main_window

    def do(self):
        if not ask_confirmation(self.__main_win.window):
            return
        SimpleAction.do(self)

        if self.__main_win.workers['ocr_redoer'].is_running:
            return
        doc = self.__main_win.doc
        self.__main_win.workers['ocr_redoer'].start(doc_target=doc)


class ActionRedoAllOCR(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Redoing doc ocr")
        self.__main_win = main_window

    def do(self):
        if not ask_confirmation(self.__main_win.window):
            return
        SimpleAction.do(self)

        if self.__main_win.workers['ocr_redoer'].is_running:
            return
        doc = self.__main_win.docsearch
        self.__main_win.workers['ocr_redoer'].start(doc_target=doc)


class BasicActionOpenExportDialog(SimpleAction):
    def __init__(self, main_window, action_txt):
        SimpleAction.__init__(self, action_txt)
        self.main_win = main_window

    def open_dialog(self, to_export):
        SimpleAction.do(self)
        self.main_win.export['estimated_size'].set_text("")
        self.main_win.export['fileFormat']['model'].clear()
        nb_export_formats = 0
        formats = to_export.get_export_formats()
        logger.info("[Export]: Supported formats: %s" % formats)
        for out_format in to_export.get_export_formats():
            self.main_win.export['fileFormat']['model'].append([out_format])
            nb_export_formats += 1
        self.main_win.export['buttons']['select_path'].set_sensitive(
            nb_export_formats >= 1)
        self.main_win.export['fileFormat']['widget'].set_active(0)
        self.main_win.export['dialog'].set_visible(True)
        self.main_win.export['buttons']['ok'].set_sensitive(False)
        self.main_win.export['export_path'].set_text("")
        self.main_win.lists['zoom_levels']['gui'].set_sensitive(False)
        self.main_win.disable_boxes()

        self.main_win.export['pageFormat']['model'].clear()
        idx = 0
        default_idx = -1
        for paper_size in Gtk.PaperSize.get_paper_sizes(True):
            store_data = (
                paper_size.get_display_name(),
                paper_size.get_width(Gtk.Unit.POINTS),
                paper_size.get_height(Gtk.Unit.POINTS)
            )
            self.main_win.export['pageFormat']['model'].append(store_data)
            if paper_size.get_name() == paper_size.get_default():
                default_idx = idx
            idx += 1
        if default_idx >= 0:
            widget = self.main_win.export['pageFormat']['widget']
            widget.set_active(default_idx)


class ActionOpenExportPageDialog(BasicActionOpenExportDialog):
    def __init__(self, main_window):
        BasicActionOpenExportDialog.__init__(self, main_window,
                                             "Displaying page export dialog")

    def do(self):
        SimpleAction.do(self)
        self.main_win.export['to_export'] = self.main_win.page
        self.main_win.export['buttons']['ok'].set_label(_("Export page"))
        BasicActionOpenExportDialog.open_dialog(self, self.main_win.page)


class ActionOpenExportDocDialog(BasicActionOpenExportDialog):
    def __init__(self, main_window):
        BasicActionOpenExportDialog.__init__(self, main_window,
                                             "Displaying page export dialog")

    def do(self):
        SimpleAction.do(self)
        self.main_win.export['to_export'] = self.main_win.doc
        self.main_win.export['buttons']['ok'].set_label(_("Export document"))
        BasicActionOpenExportDialog.open_dialog(self, self.main_win.doc)


class ActionSelectExportFormat(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Select export format")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        file_format_widget = self.__main_win.export['fileFormat']['widget']
        format_idx = file_format_widget.get_active()
        if format_idx < 0:
            return
        file_format_model = self.__main_win.export['fileFormat']['model']
        imgformat = file_format_model[format_idx][0]

        target = self.__main_win.export['to_export']
        exporter = target.build_exporter(imgformat)
        self.__main_win.export['exporter'] = exporter

        logger.info("[Export] Format: %s" % (exporter))
        logger.info("[Export] Can change quality ? %s"
               % exporter.can_change_quality)
        logger.info("[Export] Can_select_format ? %s"
               % exporter.can_select_format)

        widgets = [
            (exporter.can_change_quality,
             [
                 self.__main_win.export['quality']['widget'],
                 self.__main_win.export['quality']['label'],
             ]),
            (exporter.can_select_format,
             [
                 self.__main_win.export['pageFormat']['widget'],
                 self.__main_win.export['pageFormat']['label'],
             ]),
        ]
        for (sensitive, widgets) in widgets:
            for widget in widgets:
                widget.set_sensitive(sensitive)

        if exporter.can_change_quality or exporter.can_select_format:
            self.__main_win.actions['change_export_property'][1].do()
        else:
            size_txt = sizeof_fmt(exporter.estimate_size())
            self.__main_win.export['estimated_size'].set_text(size_txt)


class ActionChangeExportProperty(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Export property changed")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        if self.__main_win.export['exporter'].can_select_format:
            page_format_widget = self.__main_win.export['pageFormat']['widget']
            format_idx = page_format_widget.get_active()
            if (format_idx < 0):
                return
            page_format_model = self.__main_win.export['pageFormat']['model']
            (name, x, y) = page_format_model[format_idx]
            self.__main_win.export['exporter'].set_page_format((x, y))
        if self.__main_win.export['exporter'].can_change_quality:
            quality = self.__main_win.export['quality']['model'].get_value()
            self.__main_win.export['exporter'].set_quality(quality)
        self.__main_win.refresh_export_preview()


class ActionSelectExportPath(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Select export path")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        chooser = Gtk.FileChooserDialog(title=_("Save as"),
                                        parent=self.__main_win.window,
                                        action=Gtk.FileChooserAction.SAVE,
                                        buttons=(Gtk.STOCK_CANCEL,
                                                 Gtk.ResponseType.CANCEL,
                                                 Gtk.STOCK_SAVE,
                                                 Gtk.ResponseType.OK))
        file_filter = Gtk.FileFilter()
        file_filter.set_name(str(self.__main_win.export['exporter']))
        mime = self.__main_win.export['exporter'].get_mime_type()
        file_filter.add_mime_type(mime)
        chooser.add_filter(file_filter)

        response = chooser.run()
        filepath = chooser.get_filename()
        chooser.destroy()
        if response != Gtk.ResponseType.OK:
            logger.warn("File path for export canceled")
            return

        valid_exts = self.__main_win.export['exporter'].get_file_extensions()
        has_valid_ext = False
        for valid_ext in valid_exts:
            if filepath.lower().endswith(valid_ext.lower()):
                has_valid_ext = True
                break
        if not has_valid_ext:
            filepath += ".%s" % valid_exts[0]

        self.__main_win.export['export_path'].set_text(filepath)
        self.__main_win.export['buttons']['ok'].set_sensitive(True)


class BasicActionEndExport(SimpleAction):
    def __init__(self, main_win, name):
        SimpleAction.__init__(self, name)
        self.main_win = main_win

    def do(self):
        SimpleAction.do(self)
        self.main_win.lists['zoom_levels']['gui'].set_sensitive(True)
        self.main_win.export['dialog'].set_visible(False)
        self.main_win.export['exporter'] = None
        # force refresh of the current page
        self.main_win.show_page(self.main_win.page)


class ActionExport(BasicActionEndExport):
    def __init__(self, main_window):
        BasicActionEndExport.__init__(self, main_window, "Export")
        self.main_win = main_window

    def do(self):
        filepath = self.main_win.export['export_path'].get_text()
        self.main_win.export['exporter'].save(filepath)
        BasicActionEndExport.do(self)


class ActionCancelExport(BasicActionEndExport):
    def __init__(self, main_window):
        BasicActionEndExport.__init__(self, main_window, "Cancel export")

    def do(self):
        BasicActionEndExport.do(self)


class ActionSetToolbarVisibility(SimpleAction):
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Set toolbar visibility")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)
        visible = self.__main_win.show_toolbar.get_active()
        if self.__config.toolbar_visible != visible:
            self.__config.toolbar_visible = visible
        for toolbar in self.__main_win.toolbars:
            toolbar.set_visible(visible)


class ActionZoomChange(SimpleAction):
    def __init__(self, main_window, offset):
        SimpleAction.__init__(self, "Zoom += %d" % offset)
        self.__main_win = main_window
        self.__offset = offset

    def do(self):
        SimpleAction.do(self)

        zoom_liststore = self.__main_win.lists['zoom_levels']['model']

        zoom_list = [
            (zoom_liststore[zoom_idx][1], zoom_idx)
            for zoom_idx in range(0, len(zoom_liststore))
        ]
        zoom_list.append((99999.0, -1))
        zoom_list.sort()

        current_zoom = self.__main_win.get_zoom_factor()

        # figures out where the current zoom fits in the zoom list
        current_idx = -1

        for zoom_list_idx in range(0, len(zoom_list)):
            if (zoom_list[zoom_list_idx][0] == 0.0):
                continue
            logger.info("%f <= %f < %f ?" % (zoom_list[zoom_list_idx][0],
                                        current_zoom,
                                        zoom_list[zoom_list_idx+1][0]))
            if (zoom_list[zoom_list_idx][0] <= current_zoom
                    and current_zoom < zoom_list[zoom_list_idx+1][0]):
                current_idx = zoom_list_idx
                break

        assert(current_idx >= 0)

        # apply the change
        current_idx += self.__offset

        if (current_idx < 0 or current_idx >= len(zoom_liststore)):
            return

        if zoom_list[current_idx][0] == 0.0:
            return

        self.__main_win.lists['zoom_levels']['gui'].set_active(
            zoom_list[current_idx][1])


class ActionZoomSet(SimpleAction):
    def __init__(self, main_window, value):
        SimpleAction.__init__(self, ("Zoom = %f" % value))
        self.__main_win = main_window
        self.__value = value

    def do(self):
        SimpleAction.do(self)

        zoom_liststore = self.__main_win.lists['zoom_levels']['model']

        new_idx = -1
        for zoom_idx in range(0, len(zoom_liststore)):
            if (zoom_liststore[zoom_idx][1] == self.__value):
                new_idx = zoom_idx
                break
        assert(new_idx >= 0)

        self.__main_win.lists['zoom_levels']['gui'].set_active(new_idx)


class ActionEditDoc(SimpleAction):
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Edit doc")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)
        DocEditDialog(self.__main_win, self.__config, self.__main_win.doc)


class ActionAbout(SimpleAction):
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Opening about dialog")
        self.__main_win = main_window

    def do(self):
        about = AboutDialog(self.__main_win.window)
        about.show()


class ActionQuit(SimpleAction):
    """
    Quit
    """
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Quit")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)
        self.__main_win.window.destroy()

    def on_window_close_cb(self, window):
        self.do()


class ActionRealQuit(SimpleAction):
    """
    Quit
    """
    def __init__(self, main_window, config):
        SimpleAction.__init__(self, "Quit (real)")
        self.__main_win = main_window
        self.__config = config

    def do(self):
        SimpleAction.do(self)

        for worker in self.__main_win.workers.values():
            worker.stop()

        self.__config.write()
        Gtk.main_quit()

    def on_window_close_cb(self, window):
        self.do()


class ActionRebuildIndex(SimpleAction):
    def __init__(self, main_window, config, force=False):
        SimpleAction.__init__(self, "Rebuild index")
        self.__main_win = main_window
        self.__config = config
        self.__force = force
        self.__connect_handler_id = None

    def do(self):
        SimpleAction.do(self)
        self.__main_win.workers['index_reloader'].stop()
        self.__main_win.workers['doc_examiner'].stop()
        docsearch = self.__main_win.docsearch
        self.__main_win.docsearch = DummyDocSearch()
        if self.__force:
            docsearch.destroy_index()

        doc_thumbnailer = self.__main_win.workers['doc_thumbnailer']
        lbd_func = lambda worker: GObject.idle_add(
            self.__on_thumbnailing_end_cb)
        self.__connect_handler_id = doc_thumbnailer.connect(
            'doc-thumbnailing-end', lbd_func)

        self.__main_win.workers['index_reloader'].start()

    def __on_thumbnailing_end_cb(self):
        logger.info("Index loaded and thumbnailing done. Will start refreshing the"
               " index ...")
        doc_thumbnailer = self.__main_win.workers['doc_thumbnailer']
        doc_thumbnailer.disconnect(self.__connect_handler_id)
        self.__main_win.workers['doc_examiner'].stop()

        doc_examiner = self.__main_win.workers['doc_examiner']
        lbd_func = lambda examiner: GObject.idle_add(
            self.__on_doc_exam_end, examiner)
        self.__connect_handler_id = doc_examiner.connect(
            'doc-examination-end', lbd_func)
        doc_examiner.start()

    def __on_doc_exam_end(self, examiner):
        logger.info("Document examen finished. Updating index ...")
        examiner.disconnect(self.__connect_handler_id)
        logger.info("New document: %d" % len(examiner.new_docs))
        logger.info("Updated document: %d" % len(examiner.docs_changed))
        logger.info("Deleted document: %d" % len(examiner.docs_missing))

        if (len(examiner.new_docs) == 0
                and len(examiner.docs_changed) == 0
                and len(examiner.docs_missing) == 0):
            logger.info("No changes")
            return

        self.__main_win.workers['index_updater'].start(
            new_docs=examiner.new_docs,
            upd_docs=examiner.docs_changed,
            del_docs=examiner.docs_missing
        )


class ActionEditPage(SimpleAction):
    """
    Open the dialog to edit a page
    """
    def __init__(self, main_window):
        SimpleAction.__init__(self, "Edit page")
        self.__main_win = main_window

    def do(self):
        SimpleAction.do(self)
        ped = PageEditingDialog(self.__main_win, self.__main_win.page)
        todo = ped.get_changes()
        if todo == []:
            return
        logger.info("Changes to do to the page %s:" % (self.__main_win.page))
        for action in todo:
            logger.info("- %s" % action)
        self.__main_win.workers['page_editor'].start(page=self.__main_win.page,
                                                     changes=todo)


class MainWindow(object):
    def __init__(self, config):
        # used by the set_mouse_cursor() function to keep track of how many
        # threads requested a busy mouse cursor
        self.__busy_mouse_counter = 0
        self.__last_highlight_update = time.time()

        img = PIL.Image.new("RGB", (
            WorkerDocThumbnailer.THUMB_WIDTH,
            WorkerDocThumbnailer.THUMB_HEIGHT
        ), color="#CCCCCC")
        self.default_thumbnail = image2pixbuf(img)

        widget_tree = load_uifile("mainwindow.glade")

        self.window = widget_tree.get_object("mainWindow")

        self.__config = config
        self.__scan_start = 0.0

        self.docsearch = DummyDocSearch()
        self.doc = ImgDoc(self.__config.workdir)
        self.page = DummyPage(self.doc)

        self.lists = {
            'suggestions': {
                'gui': widget_tree.get_object("entrySearch"),
                'model': widget_tree.get_object("liststoreSuggestion")
            },
            'matches': {
                'gui': widget_tree.get_object("iconviewMatch"),
                'model': widget_tree.get_object("liststoreMatch"),
                'doclist': [],
                'active_idx': -1,
            },
            'pages': {
                'gui': widget_tree.get_object("iconviewPage"),
                'model': widget_tree.get_object("liststorePage"),
            },
            'labels': {
                'gui': widget_tree.get_object("treeviewLabel"),
                'model': widget_tree.get_object("liststoreLabel"),
            },
            'zoom_levels': {
                'gui': widget_tree.get_object("comboboxZoom"),
                'model': widget_tree.get_object("liststoreZoom"),
            },
        }

        search_completion = Gtk.EntryCompletion()
        search_completion.set_model(self.lists['suggestions']['model'])
        search_completion.set_text_column(0)
        search_completion.set_match_func(lambda a, b, c, d: True, None)
        self.lists['suggestions']['gui'].set_completion(search_completion)

        self.indicators = {
            'current_page': widget_tree.get_object("entryPageNb"),
            'total_pages': widget_tree.get_object("labelTotalPages"),
        }

        self.search_field = widget_tree.get_object("entrySearch")
        # done here instead of mainwindow.glade so it can be translated
        self.search_field.set_placeholder_text(_("Search"))

        self.doc_browsing = {
            'matches': widget_tree.get_object("iconviewMatch"),
            'pages': widget_tree.get_object("iconviewPage"),
            'labels': widget_tree.get_object("treeviewLabel"),
            'search': self.search_field,
        }

        self.img = {
            "image": widget_tree.get_object("imagePageImg"),
            "scrollbar": widget_tree.get_object("scrolledwindowPageImg"),
            "viewport": {
                "widget": widget_tree.get_object("viewportImg"),
                "size": (0, 0),
            },
            "eventbox": widget_tree.get_object("eventboxImg"),
            "pixbuf": None,
            "factor": 1.0,
            "original_width": 1,
            "boxes": {
                'all': [],
                'visible': [],
                'highlighted': [],
                'selected': [],
            }
        }

        self.status = {
            'progress': widget_tree.get_object("progressbar"),
            'text': widget_tree.get_object("statusbar"),
        }

        self.popup_menus = {
            'labels': (
                widget_tree.get_object("treeviewLabel"),
                widget_tree.get_object("popupmenuLabels")
            ),
            'matches': (
                widget_tree.get_object("iconviewMatch"),
                widget_tree.get_object("popupmenuMatchs")
            ),
            'pages': (
                widget_tree.get_object("iconviewPage"),
                widget_tree.get_object("popupmenuPages")
            ),
            'page': (
                widget_tree.get_object("eventboxImg"),
                widget_tree.get_object("popupmenuPage")
            ),
        }

        self.show_all_boxes = \
            widget_tree.get_object("checkmenuitemShowAllBoxes")
        self.show_toolbar = \
            widget_tree.get_object("menuitemToolbarVisible")
        self.show_toolbar.set_active(config.toolbar_visible)

        self.toolbars = [
            widget_tree.get_object("toolbarMainWin"),
            widget_tree.get_object("toolbarPage"),
        ]
        for toolbar in self.toolbars:
            toolbar.set_visible(config.toolbar_visible)

        self.export = {
            'dialog': widget_tree.get_object("infobarExport"),
            'fileFormat': {
                'widget': widget_tree.get_object("comboboxExportFormat"),
                'model': widget_tree.get_object("liststoreExportFormat"),
            },
            'pageFormat': {
                'label': widget_tree.get_object("labelPageFormat"),
                'widget': widget_tree.get_object("comboboxPageFormat"),
                'model': widget_tree.get_object("liststorePageFormat"),
            },
            'quality': {
                'label': widget_tree.get_object("labelExportQuality"),
                'widget': widget_tree.get_object("scaleQuality"),
                'model': widget_tree.get_object("adjustmentQuality"),
            },
            'estimated_size':
            widget_tree.get_object("labelEstimatedExportSize"),
            'export_path': widget_tree.get_object("entryExportPath"),
            'buttons': {
                'select_path':
                widget_tree.get_object("buttonSelectExportPath"),
                'ok': widget_tree.get_object("buttonExport"),
                'cancel': widget_tree.get_object("buttonCancelExport"),
            },
            'to_export': None,  # usually self.page or self.doc
            'exporter': None,
        }

        self.sortings = [
            (widget_tree.get_object("radiomenuitemSortByRelevance"),
             lambda docs: None),
            (widget_tree.get_object("radiomenuitemSortByScanDate"),
             sort_documents_by_date),
        ]

        self.workers = {
            'index_reloader': WorkerDocIndexLoader(self, config),
            'doc_examiner': WorkerDocExaminer(self, config),
            'index_updater': WorkerIndexUpdater(self, config),
            'searcher': WorkerDocSearcher(self, config),
            'page_thumbnailer': WorkerPageThumbnailer(self),
            'doc_thumbnailer': WorkerDocThumbnailer(self),
            'img_builder': WorkerImgBuilder(self),
            'label_updater': WorkerLabelUpdater(self),
            'label_deleter': WorkerLabelDeleter(self),
            'single_scan': WorkerSingleScan(self, config),
            'importer': WorkerImporter(self, config),
            'progress_updater': WorkerProgressUpdater(
                "main window progress bar", self.status['progress']),
            'ocr_redoer': WorkerOCRRedoer(self, config),
            'export_previewer': WorkerExportPreviewer(self),
            'page_editor': WorkerPageEditor(self, config),
        }

        self.actions = {
            'new_doc': (
                [
                    widget_tree.get_object("menuitemNew"),
                    widget_tree.get_object("toolbuttonNew"),
                ],
                ActionNewDocument(self, config),
            ),
            'open_doc': (
                [
                    widget_tree.get_object("iconviewMatch"),
                ],
                ActionOpenSelectedDocument(self)
            ),
            'open_page': (
                [
                    widget_tree.get_object("iconviewPage"),
                ],
                ActionOpenPageSelected(self)
            ),
            'select_label': (
                [
                    widget_tree.get_object("treeviewLabel"),
                ],
                ActionLabelSelected(self)
            ),
            'single_scan': (
                [
                    widget_tree.get_object("imagemenuitemScanSingle"),
                    widget_tree.get_object("toolbuttonScan"),
                    widget_tree.get_object("menuitemScanSingle"),
                ],
                ActionSingleScan(self, config)
            ),
            'multi_scan': (
                [
                    widget_tree.get_object("imagemenuitemScanFeeder"),
                    widget_tree.get_object("menuitemScanFeeder"),
                ],
                ActionMultiScan(self, config)
            ),
            'import': (
                [
                    widget_tree.get_object("menuitemImport"),
                    widget_tree.get_object("menuitemImport1"),
                ],
                ActionImport(self, config)
            ),
            'print': (
                [
                    widget_tree.get_object("menuitemPrint"),
                    widget_tree.get_object("menuitemPrint1"),
                    widget_tree.get_object("toolbuttonPrint"),
                ],
                ActionPrintDoc(self)
            ),
            'open_export_doc_dialog': (
                [
                    widget_tree.get_object("menuitemExportDoc"),
                    widget_tree.get_object("menuitemExportDoc1"),
                    widget_tree.get_object("menuitemExportDoc2"),
                ],
                ActionOpenExportDocDialog(self)
            ),
            'open_export_page_dialog': (
                [
                    widget_tree.get_object("menuitemExportPage"),
                    widget_tree.get_object("menuitemExportPage1"),
                    widget_tree.get_object("menuitemExportPage2"),
                    widget_tree.get_object("menuitemExportPage3"),
                ],
                ActionOpenExportPageDialog(self)
            ),
            'cancel_export': (
                [widget_tree.get_object("buttonCancelExport")],
                ActionCancelExport(self),
            ),
            'select_export_format': (
                [widget_tree.get_object("comboboxExportFormat")],
                ActionSelectExportFormat(self),
            ),
            'change_export_property': (
                [
                    widget_tree.get_object("scaleQuality"),
                    widget_tree.get_object("comboboxPageFormat"),
                ],
                ActionChangeExportProperty(self),
            ),
            'select_export_path': (
                [widget_tree.get_object("buttonSelectExportPath")],
                ActionSelectExportPath(self),
            ),
            'export': (
                [widget_tree.get_object("buttonExport")],
                ActionExport(self),
            ),
            'open_settings': (
                [
                    widget_tree.get_object("menuitemSettings"),
                    widget_tree.get_object("toolbuttonSettings"),
                ],
                ActionOpenSettings(self, config)
            ),
            'quit': (
                [
                    widget_tree.get_object("menuitemQuit"),
                    widget_tree.get_object("toolbuttonQuit"),
                ],
                ActionQuit(self, config),
            ),
            'create_label': (
                [
                    widget_tree.get_object("buttonAddLabel"),
                    widget_tree.get_object("menuitemAddLabel"),
                ],
                ActionCreateLabel(self),
            ),
            'edit_label': (
                [
                    widget_tree.get_object("menuitemEditLabel"),
                    widget_tree.get_object("buttonEditLabel"),
                ],
                ActionEditLabel(self),
            ),
            'del_label': (
                [
                    widget_tree.get_object("menuitemDestroyLabel"),
                    widget_tree.get_object("buttonDelLabel"),
                ],
                ActionDeleteLabel(self),
            ),
            'open_doc_dir': (
                [
                    widget_tree.get_object("menuitemOpenParentDir"),
                    widget_tree.get_object("menuitemOpenDocDir"),
                    widget_tree.get_object("toolbuttonOpenDocDir"),
                ],
                ActionOpenDocDir(self),
            ),
            'del_doc': (
                [
                    widget_tree.get_object("menuitemDestroyDoc"),
                    widget_tree.get_object("menuitemDestroyDoc2"),
                    widget_tree.get_object("toolbuttonDeleteDoc"),
                ],
                ActionDeleteDoc(self),
            ),
            'edit_page': (
                [
                    widget_tree.get_object("menuitemEditPage"),
                    widget_tree.get_object("menuitemEditPage1"),
                    widget_tree.get_object("menuitemEditPage2"),
                    widget_tree.get_object("toolbuttonEditPage"),
                ],
                ActionEditPage(self),
            ),
            'del_page': (
                [
                    widget_tree.get_object("menuitemDestroyPage"),
                    widget_tree.get_object("menuitemDestroyPage1"),
                    widget_tree.get_object("menuitemDestroyPage2"),
                    widget_tree.get_object("buttonDeletePage"),
                ],
                ActionDeletePage(self),
            ),
            'first_page': (
                [
                    widget_tree.get_object("menuitemFirstPage"),
                ],
                ActionMovePageIndex(self, False, 0),
            ),
            'prev_page': (
                [
                    widget_tree.get_object("menuitemPrevPage"),
                    widget_tree.get_object("toolbuttonPrevPage"),
                ],
                ActionMovePageIndex(self, True, -1),
            ),
            'next_page': (
                [
                    widget_tree.get_object("menuitemNextPage"),
                    widget_tree.get_object("toolbuttonNextPage"),
                ],
                ActionMovePageIndex(self, True, 1),
            ),
            'last_page': (
                [
                    widget_tree.get_object("menuitemLastPage"),
                ],
                ActionMovePageIndex(self, False, -1),
            ),
            'set_current_page': (
                [
                    widget_tree.get_object("entryPageNb"),
                ],
                ActionOpenPageNb(self),
            ),
            'zoom_levels': (
                [
                    widget_tree.get_object("comboboxZoom"),
                ],
                ActionRebuildPage(self)
            ),
            'zoom_in': (
                [
                    widget_tree.get_object("menuitemZoomIn"),
                ],
                ActionZoomChange(self, 1)
            ),
            'zoom_out': (
                [
                    widget_tree.get_object("menuitemZoomOut"),
                ],
                ActionZoomChange(self, -1)
            ),
            'zoom_best_fit': (
                [
                    widget_tree.get_object("menuitemZoomBestFit"),
                ],
                ActionZoomSet(self, 0.0)
            ),
            'zoom_normal': (
                [
                    widget_tree.get_object("menuitemZoomNormal"),
                ],
                ActionZoomSet(self, 1.0)
            ),
            'start_search': (
                [
                    widget_tree.get_object("menuitemFindTxt"),
                ],
                ActionStartSearch(self)
            ),
            'search': (
                [
                    self.search_field,
                ],
                ActionUpdateSearchResults(self),
            ),
            'switch_sorting': (
                [
                    widget_tree.get_object("radiomenuitemSortByRelevance"),
                    widget_tree.get_object("radiomenuitemSortByScanDate"),
                ],
                ActionUpdateSearchResults(self, refresh_pages=False),
            ),
            'toggle_label': (
                [
                    widget_tree.get_object("cellrenderertoggleLabel"),
                ],
                ActionToggleLabel(self),
            ),
            'show_all_boxes': (
                [
                    self.show_all_boxes
                ],
                ActionRefreshPage(self)
            ),
            'show_toolbar': (
                [
                    self.show_toolbar,
                ],
                ActionSetToolbarVisibility(self, config),
            ),
            'redo_ocr_doc': (
                [
                    widget_tree.get_object("menuitemReOcr"),
                ],
                ActionRedoDocOCR(self),
            ),
            'redo_ocr_all': (
                [
                    widget_tree.get_object("menuitemReOcrAll"),
                ],
                ActionRedoAllOCR(self),
            ),
            'reindex': (
                [],
                ActionRebuildIndex(self, config, force=False),
            ),
            'reindex_from_scratch': (
                [
                    widget_tree.get_object("menuitemReindexAll"),
                ],
                ActionRebuildIndex(self, config, force=True),
            ),
            'edit_doc': (
                [
                    widget_tree.get_object("menuitemEditDoc1"),
                    widget_tree.get_object("toolbuttonEditDoc"),
                    widget_tree.get_object("menuitemEditDoc")
                ],
                ActionEditDoc(self, config),
            ),
            'about': (
                [
                    widget_tree.get_object("menuitemAbout"),
                ],
                ActionAbout(self),
            ),
        }

        for action in self.actions:
            for button in self.actions[action][0]:
                if button is None:
                    logger.warn("MISSING BUTTON: %s" % (action))
            self.actions[action][1].connect(self.actions[action][0])

        for (buttons, action) in self.actions.values():
            for button in buttons:
                if isinstance(button, Gtk.ToolButton):
                    button.set_tooltip_text(button.get_label())

        for button in self.actions['single_scan'][0]:
            # let's be more specific on the tool tips of these buttons
            if isinstance(button, Gtk.ToolButton):
                button.set_tooltip_text(_("Scan single page"))

        self.need_doc_widgets = (
            self.actions['print'][0]
            + self.actions['create_label'][0]
            + self.actions['open_doc_dir'][0]
            + self.actions['del_doc'][0]
            + self.actions['set_current_page'][0]
            + self.actions['toggle_label'][0]
            + self.actions['redo_ocr_doc'][0]
            + self.actions['open_export_doc_dialog'][0]
            + self.actions['edit_doc'][0]
        )

        self.need_page_widgets = (
            self.actions['del_page'][0]
            + self.actions['first_page'][0]
            + self.actions['prev_page'][0]
            + self.actions['next_page'][0]
            + self.actions['last_page'][0]
            + self.actions['open_export_page_dialog'][0]
            + self.actions['edit_page'][0]
        )

        self.need_label_widgets = (
            self.actions['del_label'][0]
            + self.actions['edit_label'][0]
        )

        self.doc_edit_widgets = (
            self.actions['single_scan'][0]
            + self.actions['del_page'][0]
            + self.actions['edit_page'][0]
        )

        for widget in self.need_doc_widgets + self.need_page_widgets:
            widget.set_sensitive(False)

        for (popup_menu_name, popup_menu) in self.popup_menus.iteritems():
            assert(not popup_menu[0] is None)
            assert(not popup_menu[1] is None)
            # TODO(Jflesch): Find the correct signal
            # This one doesn't take into account the key to access these menus
            popup_menu[0].connect("button-press-event", self.__popup_menu_cb,
                                  popup_menu[0], popup_menu[1])

        self.img['eventbox'].add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        self.img['eventbox'].connect("leave-notify-event",
                                     self.__on_img_mouse_leave)
        self.img['eventbox'].connect("motion-notify-event",
                                     self.__on_img_mouse_motion)

        for widget in [self.lists['pages']['gui'],
                       self.lists['matches']['gui']]:
            widget.enable_model_drag_dest([], Gdk.DragAction.MOVE)
            widget.drag_dest_add_text_targets()

        self.lists['pages']['gui'].connect(
            "drag-data-get", self.__on_page_list_drag_data_get_cb)
        self.lists['pages']['gui'].connect(
            "drag-data-received", self.__on_page_list_drag_data_received_cb)
        self.lists['matches']['gui'].connect(
            "drag-data-received", self.__on_match_list_drag_data_received_cb)

        self.window.connect("destroy",
                            ActionRealQuit(self, config).on_window_close_cb)

        self.workers['index_reloader'].connect(
            'index-loading-start',
            lambda loader: GObject.idle_add(self.__on_index_loading_start_cb,
                                            loader))
        self.workers['index_reloader'].connect(
            'index-loading-progression',
            lambda loader, progression, txt:
            GObject.idle_add(self.set_progression, loader,
                             progression, txt))
        self.workers['index_reloader'].connect(
            'index-loading-end',
            lambda loader:
            GObject.idle_add(self.__on_index_loading_end_cb, loader))

        self.workers['doc_examiner'].connect(
            'doc-examination-start',
            lambda examiner:
            GObject.idle_add(self.__on_doc_examination_start_cb, examiner))
        self.workers['doc_examiner'].connect(
            'doc-examination-progression',
            lambda examiner, progression, txt:
            GObject.idle_add(self.set_progression, examiner,
                             progression, txt))
        self.workers['doc_examiner'].connect(
            'doc-examination-end',
            lambda examiner: GObject.idle_add(
                self.__on_doc_examination_end_cb, examiner))

        self.workers['index_updater'].connect(
            'index-update-start',
            lambda updater:
            GObject.idle_add(self.__on_index_update_start_cb, updater))
        self.workers['index_updater'].connect(
            'index-update-progression',
            lambda updater, progression, txt:
            GObject.idle_add(self.set_progression, updater, progression, txt))
        self.workers['index_updater'].connect(
            'index-update-end',
            lambda updater:
            GObject.idle_add(self.__on_index_update_end_cb, updater))
        self.workers['searcher'].connect(
            'search-result',
            lambda searcher, documents, suggestions:
            GObject.idle_add(self.__on_search_result_cb, documents,
                             suggestions))

        self.workers['page_thumbnailer'].connect(
            'page-thumbnailing-start',
            lambda thumbnailer:
            GObject.idle_add(self.__on_page_thumbnailing_start_cb,
                             thumbnailer))
        self.workers['page_thumbnailer'].connect(
            'page-thumbnailing-page-done',
            lambda thumbnailer, page_idx, thumbnail:
            GObject.idle_add(self.__on_page_thumbnailing_page_done_cb,
                             thumbnailer, page_idx, thumbnail))
        self.workers['page_thumbnailer'].connect(
            'page-thumbnailing-end',
            lambda thumbnailer:
            GObject.idle_add(self.__on_page_thumbnailing_end_cb,
                             thumbnailer))

        self.workers['doc_thumbnailer'].connect(
            'doc-thumbnailing-start',
            lambda thumbnailer:
            GObject.idle_add(self.__on_doc_thumbnailing_start_cb,
                             thumbnailer))
        self.workers['doc_thumbnailer'].connect(
            'doc-thumbnailing-doc-done',
            lambda thumbnailer, doc_idx, thumbnail:
            GObject.idle_add(self.__on_doc_thumbnailing_doc_done_cb,
                             thumbnailer, doc_idx, thumbnail))
        self.workers['doc_thumbnailer'].connect(
            'doc-thumbnailing-end',
            lambda thumbnailer:
            GObject.idle_add(self.__on_doc_thumbnailing_end_cb,
                             thumbnailer))

        self.workers['img_builder'].connect(
            'img-building-start',
            lambda builder:
            GObject.idle_add(self.__on_img_building_start))
        self.workers['img_builder'].connect(
            'img-building-result-pixbuf',
            lambda builder, factor, original_width, img, boxes:
            GObject.idle_add(self.__on_img_building_result_pixbuf,
                             builder, factor, original_width, img, boxes))
        self.workers['img_builder'].connect(
            'img-building-result-stock',
            lambda builder, img:
            GObject.idle_add(self.__on_img_building_result_stock, img))
        self.workers['img_builder'].connect(
            'img-building-result-clear',
            lambda builder:
            GObject.idle_add(self.__on_img_building_result_clear))

        self.workers['label_updater'].connect(
            'label-updating-start',
            lambda updater:
            GObject.idle_add(self.__on_label_updating_start_cb,
                             updater))
        self.workers['label_updater'].connect(
            'label-updating-doc-updated',
            lambda updater, progression, doc_name:
            GObject.idle_add(self.__on_label_updating_doc_updated_cb,
                             updater, progression, doc_name))
        self.workers['label_updater'].connect(
            'label-updating-end',
            lambda updater:
            GObject.idle_add(self.__on_label_updating_end_cb,
                             updater))

        self.workers['label_deleter'].connect(
            'label-deletion-start',
            lambda deleter:
            GObject.idle_add(self.__on_label_updating_start_cb,
                             deleter))
        self.workers['label_deleter'].connect(
            'label-deletion-doc-updated',
            lambda deleter, progression, doc_name:
            GObject.idle_add(self.__on_label_deletion_doc_updated_cb,
                             deleter, progression, doc_name))
        self.workers['label_deleter'].connect(
            'label-deletion-end',
            lambda deleter:
            GObject.idle_add(self.__on_label_updating_end_cb,
                             deleter))

        self.workers['ocr_redoer'].connect(
            'redo-ocr-start',
            lambda ocr_redoer:
            GObject.idle_add(self.__on_redo_ocr_start_cb,
                             ocr_redoer))
        self.workers['ocr_redoer'].connect(
            'redo-ocr-doc-updated',
            lambda ocr_redoer, progression, doc_name:
            GObject.idle_add(self.__on_redo_ocr_doc_updated_cb,
                             ocr_redoer, progression, doc_name))
        self.workers['ocr_redoer'].connect(
            'redo-ocr-end',
            lambda ocr_redoer:
            GObject.idle_add(self.__on_redo_ocr_end_cb,
                             ocr_redoer))

        self.workers['single_scan'].connect(
            'single-scan-start',
            lambda worker:
            GObject.idle_add(self.__on_single_scan_start, worker))
        self.workers['single_scan'].connect(
            'single-scan-ocr',
            lambda worker:
            GObject.idle_add(self.__on_single_scan_ocr, worker))
        self.workers['single_scan'].connect(
            'single-scan-done',
            lambda worker, page:
            GObject.idle_add(self.__on_single_scan_done, worker, page))

        self.workers['importer'].connect(
            'import-start',
            lambda worker:
            GObject.idle_add(self.__on_import_start, worker))
        self.workers['importer'].connect(
            'import-done',
            lambda worker, doc, page:
            GObject.idle_add(self.__on_import_done, worker, doc, page))

        self.workers['export_previewer'].connect(
            'export-preview-start',
            lambda worker:
            GObject.idle_add(self.__on_export_preview_start))
        self.workers['export_previewer'].connect(
            'export-preview-done',
            lambda worker, size, pixbuf:
            GObject.idle_add(self.__on_export_preview_done, size,
                             pixbuf))

        self.workers['page_editor'].connect(
            'page-editing-img-edit',
            lambda worker, page:
            GObject.idle_add(self.__on_page_editing_img_edit_start_cb,
                             worker, page))
        self.workers['page_editor'].connect(
            'page-editing-ocr',
            lambda worker, page:
            GObject.idle_add(self.__on_page_editing_ocr_cb,
                             worker, page))
        self.workers['page_editor'].connect(
            'page-editing-index-upd',
            lambda worker, page:
            GObject.idle_add(self.__on_page_editing_index_upd_cb,
                             worker, page))
        self.workers['page_editor'].connect(
            'page-editing-done',
            lambda worker, page:
            GObject.idle_add(self.__on_page_editing_done_cb,
                             worker, page))

        self.img['image'].connect_after('draw', self.__on_img_draw)

        self.img['viewport']['widget'].connect("size-allocate",
                                               self.__on_img_resize_cb)

        self.window.set_visible(True)

    def set_search_availability(self, enabled):
        for list_view in self.doc_browsing.values():
            list_view.set_sensitive(enabled)

    def set_mouse_cursor(self, cursor):
        offset = {
            "Normal": -1,
            "Busy": 1
        }[cursor]

        self.__busy_mouse_counter += offset
        assert(self.__busy_mouse_counter >= 0)

        if self.__busy_mouse_counter > 0:
            cursor = Gdk.Cursor.new(Gdk.CursorType.WATCH)
        else:
            cursor = None
        self.window.get_window().set_cursor(cursor)

    def set_progression(self, src, progression, text):
        context_id = self.status['text'].get_context_id(str(src))
        self.status['text'].pop(context_id)
        if (text is not None and text != ""):
            self.status['text'].push(context_id, text)
        self.status['progress'].set_fraction(progression)

    def __on_index_loading_start_cb(self, src):
        self.set_progression(src, 0.0, None)
        self.set_search_availability(False)
        self.set_mouse_cursor("Busy")

    def __on_index_loading_end_cb(self, src):
        self.set_progression(src, 0.0, None)
        self.set_search_availability(True)
        self.set_mouse_cursor("Normal")
        self.refresh_doc_list()
        self.refresh_label_list()

    def __on_doc_examination_start_cb(self, src):
        self.set_progression(src, 0.0, None)

    def __on_doc_examination_end_cb(self, src):
        self.set_progression(src, 0.0, None)

    def __on_index_update_start_cb(self, src):
        self.set_progression(src, 0.0, None)
        self.set_search_availability(False)
        self.set_mouse_cursor("Busy")

    def __on_index_update_end_cb(self, src):
        self.workers['index_reloader'].stop()
        self.set_progression(src, 0.0, None)
        self.set_search_availability(True)
        self.set_mouse_cursor("Normal")
        self.workers['index_reloader'].start()

    def __on_search_result_cb(self, documents, suggestions):
        self.workers['page_thumbnailer'].soft_stop()
        self.workers['doc_thumbnailer'].stop()

        logger.debug("Got %d suggestions" % len(suggestions))
        self.lists['suggestions']['model'].clear()
        for suggestion in suggestions:
            self.lists['suggestions']['model'].append([suggestion])

        logger.debug("Got %d documents" % len(documents))
        self.lists['matches']['model'].clear()
        active_idx = -1
        idx = 0
        for doc in documents:
            if doc == self.doc:
                active_idx = idx
            idx += 1
            self.lists['matches']['model'].append(
                self.__get_doc_model_line(doc))

        if len(documents) > 0 and documents[0].is_new and self.doc.is_new:
            active_idx = 0

        self.lists['matches']['doclist'] = documents
        self.lists['matches']['active_idx'] = active_idx

        self.__select_doc(active_idx)

        self.workers['page_thumbnailer'].stop()
        self.workers['page_thumbnailer'].start()
        self.workers['doc_thumbnailer'].start()

    def __on_page_thumbnailing_start_cb(self, src):
        self.set_progression(src, 0.0, _("Loading thumbnails ..."))
        self.set_mouse_cursor("Busy")

    def __on_page_thumbnailing_page_done_cb(self, src, page_idx, thumbnail):
        line_iter = self.lists['pages']['model'].get_iter(page_idx)
        self.lists['pages']['model'].set_value(line_iter, 0, thumbnail)
        self.set_progression(src, ((float)(page_idx+1) / self.doc.nb_pages),
                             _("Loading thumbnails ..."))

    def __on_page_thumbnailing_end_cb(self, src):
        self.set_progression(src, 0.0, None)
        self.set_mouse_cursor("Normal")

    def __on_doc_thumbnailing_start_cb(self, src):
        self.set_progression(src, 0.0, _("Loading thumbnails ..."))

    def __on_doc_thumbnailing_doc_done_cb(self, src, doc_idx, thumbnail):
        line_iter = self.lists['matches']['model'].get_iter(doc_idx)
        self.lists['matches']['model'].set_value(line_iter, 2, thumbnail)
        self.set_progression(src, ((float)(doc_idx+1) /
                                   len(self.lists['matches']['doclist'])),
                             _("Loading thumbnails ..."))
        active_doc_idx = self.lists['matches']['active_idx']

    def __on_doc_thumbnailing_end_cb(self, src):
        self.set_progression(src, 0.0, None)

    def disable_boxes(self):
        self.img['boxes']['all'] = []
        self.img['boxes']['highlighted'] = []
        self.img['boxes']['visible'] = []

    def __on_img_building_start(self):
        self.disable_boxes()
        self.set_mouse_cursor("Busy")
        self.img['image'].set_from_stock(Gtk.STOCK_EXECUTE,
                                         Gtk.IconSize.DIALOG)

    def __on_img_building_result_stock(self, img):
        self.img['image'].set_from_stock(img, Gtk.IconSize.DIALOG)
        self.set_mouse_cursor("Normal")

    def __on_img_building_result_clear(self):
        self.img['image'].clear()
        self.set_mouse_cursor("Normal")

    def __on_img_building_result_pixbuf(self, builder, factor, original_width,
                                        pixbuf, boxes):
        self.img['boxes']['all'] = boxes
        self.__reload_boxes()

        self.img['factor'] = factor
        self.img['pixbuf'] = pixbuf
        self.img['original_width'] = original_width

        self.img['image'].set_from_pixbuf(pixbuf)
        self.set_mouse_cursor("Normal")

    def __on_label_updating_start_cb(self, src):
        self.set_search_availability(False)
        self.set_mouse_cursor("Busy")

    def __on_label_updating_doc_updated_cb(self, src, progression, doc_name):
        self.set_progression(src, progression,
                             _("Updating label (%s) ...") % (doc_name))

    def __on_label_deletion_doc_updated_cb(self, src, progression, doc_name):
        self.set_progression(src, progression,
                             _("Deleting label (%s) ...") % (doc_name))

    def __on_label_updating_end_cb(self, src):
        self.set_progression(src, 0.0, None)
        self.set_search_availability(True)
        self.set_mouse_cursor("Normal")
        self.refresh_label_list()

    def __on_redo_ocr_start_cb(self, src):
        self.set_search_availability(False)
        self.set_mouse_cursor("Busy")
        self.set_progression(src, 0.0, _("Redoing OCR ..."))

    def __on_redo_ocr_doc_updated_cb(self, src, progression, doc_name):
        self.set_progression(src, progression,
                             _("Redoing OCR (%s) ...") % (doc_name))

    def __on_redo_ocr_end_cb(self, src):
        self.set_progression(src, 0.0, None)
        self.set_search_availability(True)
        self.set_mouse_cursor("Normal")
        self.refresh_label_list()
        # in case the keywords were highlighted
        self.show_page(self.page)
        self.actions['reindex'][1].do()

    def __on_single_scan_start(self, src):
        self.set_progression(src, 0.0, _("Scanning ..."))
        self.set_mouse_cursor("Busy")
        self.img['image'].set_from_stock(Gtk.STOCK_EXECUTE,
                                         Gtk.IconSize.DIALOG)
        for widget in self.doc_edit_widgets:
            widget.set_sensitive(False)
        self.__scan_start = time.time()
        self.workers['progress_updater'].start(
            value_min=0.0, value_max=0.5,
            total_time=self.__config.scan_time['normal'])

    def __on_single_scan_ocr(self, src):
        scan_stop = time.time()
        self.workers['progress_updater'].stop()
        self.__config.scan_time['normal'] = scan_stop - self.__scan_start

        self.set_progression(src, 0.5, _("Reading ..."))

        self.__scan_start = time.time()
        self.workers['progress_updater'].start(
            value_min=0.5, value_max=1.0,
            total_time=self.__config.scan_time['ocr'])

    def __on_single_scan_done(self, src, page):
        scan_stop = time.time()
        self.__config.scan_time['ocr'] = scan_stop - self.__scan_start

        for widget in self.need_doc_widgets:
            widget.set_sensitive(True)
        for widget in self.doc_edit_widgets:
            widget.set_sensitive(True)

        self.set_progression(src, 0.0, None)
        self.set_mouse_cursor("Normal")
        self.refresh_page_list()

        assert(page is not None)
        self.show_page(page)

        self.append_docs([self.doc])

        self.workers['progress_updater'].stop()

    def __on_import_start(self, src):
        self.set_progression(src, 0.0, _("Importing ..."))
        self.set_mouse_cursor("Busy")
        self.img['image'].set_from_stock(Gtk.STOCK_EXECUTE,
                                         Gtk.IconSize.DIALOG)
        self.workers['progress_updater'].start(
            value_min=0.0, value_max=0.75,
            total_time=self.__config.scan_time['ocr'])
        self.__scan_start = time.time()

    def __on_import_done(self, src, doc, page=None):
        scan_stop = time.time()
        self.workers['progress_updater'].stop()
        # Note: don't update scan time here: OCR is not required for all
        # imports

        for widget in self.need_doc_widgets:
            widget.set_sensitive(True)

        self.set_progression(src, 0.0, None)
        self.set_mouse_cursor("Normal")
        self.show_doc(doc)  # will refresh the page list
        # Many documents may have been imported actually. So we still
        # refresh the whole list
        self.refresh_doc_list()
        if page is not None:
            self.show_page(page)

    def __popup_menu_cb(self, ev_component, event, ui_component, popup_menu):
        # we are only interested in right clicks
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return
        popup_menu.popup(None, None, None, None, event.button, event.time)

    def __on_img_mouse_motion(self, event_box, event):
        (mouse_x, mouse_y) = event.get_coords()

        # prevent looking for boxes all the time
        # XXX(Jflesch): This is a hack .. it may have visible side effects
        # in the GUI ...
        now = time.time()
        if (now - self.__last_highlight_update <= 0.25):
            return
        self.__last_highlight_update = now

        to_refresh = self.img['boxes']['selected']
        selected = None

        for line in self.img['boxes']['all']:
            pos = self.__get_box_position(line,
                                          window=self.img['image'],
                                          width=0)
            ((a, b), (c, d)) = pos
            if (mouse_x < a or mouse_y < b
                    or mouse_x > c or mouse_y > d):
                continue
            for box in line.word_boxes:
                pos = self.__get_box_position(box,
                                              window=self.img['image'],
                                              width=0)
                ((a, b), (c, d)) = pos
                if (mouse_x < a or mouse_y < b
                        or mouse_x > c or mouse_y > d):
                    continue
                selected = box
                break

        if selected is not None:
            if selected in self.img['boxes']['selected']:
                return
            to_refresh.append(selected)

        if selected is not None:
            self.img['boxes']['selected'] = [selected]
            self.img['image'].set_tooltip_text(selected.content)
        else:
            self.img['boxes']['selected'] = []
            self.img['image'].set_has_tooltip(False)

        for box in to_refresh:
            position = self.__get_box_position(
                box, window=self.img['image'], width=5)
            self.img['image'].queue_draw_area(position[0][0], position[0][1],
                                              position[1][0] - position[0][0],
                                              position[1][1] - position[0][1])

    def __on_img_mouse_leave(self, event_box, event):
        to_refresh = self.img['boxes']['selected']

        self.img['boxes']['selected'] = []
        self.img['image'].set_has_tooltip(False)

        for box in to_refresh:
            position = self.__get_box_position(
                box, window=self.img['image'], width=5)
            self.img['image'].queue_draw_area(position[0][0], position[0][1],
                                              position[1][0] - position[0][0],
                                              position[1][1] - position[0][1])

    def __get_box_position(self, box, window=None, width=1):
        ((a, b), (c, d)) = box.position
        a *= self.img['factor']
        b *= self.img['factor']
        c *= self.img['factor']
        d *= self.img['factor']
        if window:
            (win_w, win_h) = (window.get_allocation().width,
                              window.get_allocation().height)
            (pic_w, pic_h) = (self.img['pixbuf'].get_width(),
                              self.img['pixbuf'].get_height())
            (margin_x, margin_y) = ((win_w-pic_w)/2, (win_h-pic_h)/2)
            a += margin_x
            b += margin_y
            c += margin_x
            d += margin_y
        a -= width
        b -= width
        c += width
        d += width
        return ((int(a), int(b)), (int(c), int(d)))

    def __on_img_draw(self, imgwidget, cairo_context):
        visible = []
        for line in self.img['boxes']['visible']:
            visible += line.word_boxes
        colors = [
            ((0.421875, 0.36328125, 0.81640625), 1, visible),
            ((0.421875, 0.36328125, 0.81640625), 2,
             self.img['boxes']['selected']),
            ((0.0, 0.62109375, 0.0), 2, self.img['boxes']['highlighted'])
        ]
        for ((color_r, color_b, color_g), line_width, boxes) in colors:
            cairo_context.set_source_rgb(color_r, color_b, color_g)
            cairo_context.set_line_width(line_width)

            for box in boxes:
                ((a, b), (c, d)) = self.__get_box_position(box, imgwidget,
                                                           width=line_width)
                cairo_context.rectangle(a, b, c-a, d-b)
                cairo_context.stroke()

    @staticmethod
    def __get_doc_txt(doc):
        if doc is None:
            return ""
        labels = doc.labels
        final_str = "%s" % (doc.name)
        nb_pages = doc.nb_pages
        if nb_pages > 1:
            final_str += (_(" (%d pages)") % (doc.nb_pages))
        if len(labels) > 0:
            final_str += "\n  "
            final_str += "\n  ".join([x.get_html() for x in labels])
        return final_str

    def __get_doc_model_line(self, doc):
        doc_txt = self.__get_doc_txt(doc)
        thumbnail = self.default_thumbnail
        if doc.nb_pages <= 0:
            thumbnail = None
        return ([
            doc_txt,
            doc,
            thumbnail,
            None,
            Gtk.IconSize.DIALOG,
        ])

    def __select_doc(self, doc_idx):
        if doc_idx >= 0:
            # we are going to select the current page in the list
            # except we don't want to be called again because of it
            self.actions['open_doc'][1].enabled = False

            self.lists['matches']['gui'].unselect_all()
            self.lists['matches']['gui'].select_path(Gtk.TreePath(doc_idx))

            self.actions['open_doc'][1].enabled = True

            # HACK(Jflesch): The Gtk documentation says that scroll_to_cell()
            # should do nothing if the target cell is already visible (which
            # is the desired behavior here). Except we just emptied the
            # document list model and remade it from scratch. For some reason,
            # it seems that  Gtk will then always consider that the cell is
            # not visible and move the scrollbar.
            # --> we use idle_add to move the scrollbar only once everything
            # has been displayed
            path = Gtk.TreePath(doc_idx)
            GObject.idle_add(self.lists['matches']['gui'].scroll_to_path,
                             path, False, 0.0, 0.0)
        else:
            self.lists['matches']['gui'].unselect_all()
            path = Gtk.TreePath(0)
            GObject.idle_add(self.lists['matches']['gui'].scroll_to_path,
                             path, False, 0.0, 0.0)

    def __insert_new_doc(self):
        sentence = unicode(self.search_field.get_text(), encoding='utf-8')
        logger.info("Search: %s" % (sentence.encode('utf-8', 'replace')))

        doc_list = self.lists['matches']['doclist']

        # When a scan is done, we try to refresh only the current document.
        # However, the current document may be "New document". In which case
        # it won't appear as "New document" anymore. So we have to add a new
        # one to the list
        if sentence == u"" and (len(doc_list) == 0 or not doc_list[0].is_new):
            # append a new document to the list
            new_doc = ImgDoc(self.__config.workdir)
            doc_list.insert(0, new_doc)
            new_doc_line = self.__get_doc_model_line(new_doc)
            self.lists['matches']['model'].insert(0, new_doc_line)
            return True
        return False

    def append_docs(self, docs):
        # We don't stop the doc thumbnailer here. It might be
        # refreshing other documents we won't
        self.workers['doc_thumbnailer'].wait()

        doc_list = self.lists['matches']['doclist']
        model = self.lists['matches']['model']

        if (len(doc_list) > 0
                and (doc_list[0] in docs or doc_list[0].is_new)):
            # Remove temporarily "New document" from the list
            doc_list.pop(0)
            model.remove(model[0].iter)

        for doc in docs:
            if doc in doc_list:
                # already in the list --> won't append
                docs.remove(doc)

        if len(docs) <= 0:
            return

        active_idx = -1
        for doc in docs:
            if doc == self.doc:
                active_idx = 0
            elif active_idx >= 0:
                active_idx += 1
            doc_list.insert(0, doc)
            doc_line = self.__get_doc_model_line(doc)
            model.insert(0, doc_line)

        max_thumbnail_idx = len(docs)
        if self.__insert_new_doc():
            if active_idx >= 0:
                active_idx += 1
            max_thumbnail_idx += 1

        if active_idx >= 0:
            self.__select_doc(active_idx)

        self.workers['doc_thumbnailer'].start(
            doc_indexes=range(0, max_thumbnail_idx))

    def refresh_docs(self, docs):
        """
        Refresh specific documents in the document list

        Arguments:
            docs --- Array of Doc
        """
        # We don't stop the doc thumbnailer here. It might be
        # refreshing other documents we won't
        self.workers['doc_thumbnailer'].wait()

        doc_list = self.lists['matches']['doclist']

        self.__insert_new_doc()

        doc_indexes = []
        active_idx = -1

        for doc in docs:
            try:
                doc_idx = doc_list.index(doc)
            except ValueError, err:
                logger.error("Warning: Should refresh doc %s in doc list, but"
                       " didn't find it !" % doc)
                continue
            doc_indexes.append(doc_idx)
            if self.doc == doc:
                active_idx = doc_idx
            doc_txt = self.__get_doc_txt(doc)
            doc_line = self.__get_doc_model_line(doc)
            self.lists['matches']['model'][doc_idx] = doc_line

        if active_idx >= 0:
            self.__select_doc(active_idx)

        self.workers['doc_thumbnailer'].start(doc_indexes=doc_indexes)

    def refresh_doc_list(self):
        """
        Update the suggestions list and the matching documents list based on
        the keywords typed by the user in the search field.
        Warning: Will reset all the thumbnail to the default one
        """
        self.workers['doc_thumbnailer'].soft_stop()
        self.workers['searcher'].soft_stop()
        self.workers['searcher'].start()

    def refresh_page_list(self):
        """
        Reload and refresh the page list.
        Warning: Will remove the thumbnails on all the pages
        """
        self.workers['page_thumbnailer'].stop()
        self.lists['pages']['model'].clear()
        for page in self.doc.pages:
            self.lists['pages']['model'].append([
                self.default_thumbnail,
                None,
                Gtk.IconSize.DIALOG,
                _('Page %d') % (page.page_nb + 1),
                page.page_nb
            ])
        self.indicators['total_pages'].set_text(
            _("/ %d") % (self.doc.nb_pages))
        for widget in self.doc_edit_widgets:
            widget.set_sensitive(self.doc.can_edit)
        for widget in self.need_page_widgets:
            widget.set_sensitive(False)
        self.workers['page_thumbnailer'].start()

    def refresh_label_list(self):
        """
        Reload and refresh the label list
        """
        self.lists['labels']['model'].clear()
        labels = self.doc.labels
        for label in self.docsearch.label_list:
            self.lists['labels']['model'].append([
                label.get_html(),
                (label in labels),
                label,
                True
            ])
        for widget in self.need_label_widgets:
            widget.set_sensitive(False)

    def __reload_boxes(self):
        search = unicode(self.search_field.get_text(), encoding='utf-8')
        self.img['boxes']['highlighted'] = self.page.get_boxes(search)
        if self.show_all_boxes.get_active():
            self.img['boxes']['visible'] = self.img['boxes']['all']
        else:
            self.img['boxes']['visible'] = []

    def refresh_page(self):
        self.__reload_boxes()
        self.img['image'].queue_draw()

    def show_page(self, page):
        logging.info("Showing page %s" % page)

        self.workers['img_builder'].stop()

        if self.export['exporter'] is not None:
            logging.info("Canceling export")
            self.actions['cancel_export'][1].do()

        for widget in self.need_page_widgets:
            widget.set_sensitive(True)
        for widget in self.doc_edit_widgets:
            widget.set_sensitive(self.doc.can_edit)

        if page.page_nb >= 0:
            # we are going to select the current page in the list
            # except we don't want to be called again because of it
            self.actions['open_page'][1].enabled = False
            path = Gtk.TreePath(page.page_nb)
            self.lists['pages']['gui'].select_path(path)
            self.lists['pages']['gui'].scroll_to_path(path, False, 0.0, 0.0)
            self.actions['open_page'][1].enabled = True

        # we are going to update the page number
        # except we don't want to be called again because of this update
        self.actions['set_current_page'][1].enabled = False
        self.indicators['current_page'].set_text("%d" % (page.page_nb + 1))
        self.actions['set_current_page'][1].enabled = True

        self.page = page

        self.export['dialog'].set_visible(False)

        self.workers['img_builder'].start()

    def show_doc(self, doc):
        self.doc = doc
        is_new = self.doc.is_new
        for widget in self.need_doc_widgets:
            widget.set_sensitive(not is_new)
        for widget in self.doc_edit_widgets:
            widget.set_sensitive(self.doc.can_edit)
        pages_gui = self.lists['pages']['gui']
        if self.doc.can_edit:
            pages_gui.enable_model_drag_source(0, [], Gdk.DragAction.MOVE)
            pages_gui.drag_source_add_text_targets()
        else:
            pages_gui.unset_model_drag_source()
        self.refresh_page_list()
        self.refresh_label_list()
        if self.doc.nb_pages > 0:
            self.show_page(self.doc.pages[0])
        else:
            self.img['image'].set_from_stock(Gtk.STOCK_MISSING_IMAGE,
                                             Gtk.IconSize.DIALOG)

    def __on_export_preview_start(self):
        self.export['estimated_size'].set_text(_("Computing ..."))

    def __on_export_preview_done(self, img_size, pixbuf):
        self.export['estimated_size'].set_text(sizeof_fmt(img_size))
        self.img['image'].set_from_pixbuf(pixbuf)

    def __get_img_area_width(self):
        return self.img['viewport']['widget'].get_allocation().width

    def get_zoom_factor(self, pixbuf_width=None):
        el_idx = self.lists['zoom_levels']['gui'].get_active()
        el_iter = self.lists['zoom_levels']['model'].get_iter(el_idx)
        factor = self.lists['zoom_levels']['model'].get_value(el_iter, 1)
        if factor != 0.0:
            return factor
        wanted_width = self.__get_img_area_width()
        if pixbuf_width is None:
            pixbuf_width = self.img['original_width']
        return float(wanted_width) / pixbuf_width

    def refresh_export_preview(self):
        self.img['image'].set_from_stock(Gtk.STOCK_EXECUTE,
                                         Gtk.IconSize.DIALOG)
        self.workers['export_previewer'].stop()
        self.workers['export_previewer'].start()

    def __on_img_resize_cb(self, viewport, rectangle):
        if self.export['exporter'] is not None:
            return

        old_size = self.img['viewport']['size']
        new_size = (rectangle.width, rectangle.height)
        if old_size == new_size:
            return

        self.workers['img_builder'].soft_stop()
        self.img['viewport']['size'] = new_size
        logger.info("Image view port resized. (%d, %d) --> (%d, %d)"
               % (old_size[0], old_size[1], new_size[0], new_size[1]))

        # check if zoom level is set to adjusted, if yes,
        # we must resize the image
        el_idx = self.lists['zoom_levels']['gui'].get_active()
        el_iter = self.lists['zoom_levels']['model'].get_iter(el_idx)
        factor = self.lists['zoom_levels']['model'].get_value(el_iter, 1)
        if factor != 0.0:
            return

        self.workers['img_builder'].start()

    def __on_page_editing_img_edit_start_cb(self, worker, page):
        self.set_mouse_cursor("Busy")
        self.set_progression(worker, 0.0, _("Updating the image ..."))

    def __on_page_editing_ocr_cb(self, worker, page):
        self.set_progression(worker, 0.25, _("Redoing OCR ..."))

    def __on_page_editing_index_upd_cb(self, worker, page):
        self.set_progression(worker, 0.75, _("Updating the index ..."))

    def __on_page_editing_done_cb(self, worker, page):
        self.set_progression(worker, 0.0, "")
        self.set_mouse_cursor("Normal")
        if page.page_nb == 0:
            self.refresh_doc_list()
        self.refresh_page_list()
        self.show_page(page)

    def __on_page_list_drag_data_get_cb(self, widget, drag_context,
                                        selection_data, info, time):
        pageid = unicode(self.page.pageid)
        logger.info("[page list] drag-data-get: %s" % self.page.pageid)
        selection_data.set_text(pageid, -1)

    def __on_page_list_drag_data_received_cb(self, widget, drag_context, x, y,
                                             selection_data, info, time):
        target = self.lists['pages']['gui'].get_dest_item_at_pos(x, y)
        if target is None:
            logger.warn("[page list] drag-data-received: no target. aborting")
            drag_context.finish(False, False, time)
            return
        (target_path, position) = target
        target_idx = self.lists['pages']['model'][target_path][4]
        if position == Gtk.IconViewDropPosition.DROP_BELOW:
            target_idx += 1
        obj_id = selection_data.get_text()

        logger.info("[page list] drag-data-received: %s -> %s" % (obj_id, target_idx))
        obj = self.docsearch.get_by_id(obj_id)
        # TODO(Jflesch): Instantiate an ActionXXX to do that, so
        # this action can be cancelled later
        obj.change_index(target_idx)

        drag_context.finish(True, False, time)
        GObject.idle_add(self.refresh_page_list)
        GObject.idle_add(self.refresh_docs, [obj.doc])

    def __on_match_list_drag_data_received_cb(self, widget, drag_context, x, y,
                                              selection_data, info, time):
        obj_id = selection_data.get_text()
        target = self.lists['matches']['gui'].get_dest_item_at_pos(x, y)
        if target is None:
            logger.warn("[page list] drag-data-received: no target. aborting")
            drag_context.finish(False, False, time)
            return
        (target_path, position) = target
        target_doc = self.lists['matches']['model'][target_path][1]
        obj_id = selection_data.get_text()
        obj = self.docsearch.get_by_id(obj_id)

        if not target_doc.can_edit:
            logger.warn("[doc list] drag-data-received: Destination document"
                   " can't be modified")
            drag_context.finish(False, False, time)
            return

        if target_doc == obj.doc:
            logger.info("[doc list] drag-data-received: Source and destination docs"
                   " are the same. Nothing to do")
            drag_context.finish(False, False, time)
            return

        logger.info("[doc list] drag-data-received: %s -> %s"
               % (obj_id, target_doc.docid))
        # TODO(Jflesch): Instantiate an ActionXXX to do that, so
        # it can be cancelled later
        target_doc.steal_page(obj)

        if obj.doc.nb_pages <= 0:
            del_docs = [obj.doc.docid]
            upd_docs = [target_doc]
        else:
            del_docs = []
            upd_docs = [obj.doc, target_doc]

        drag_context.finish(True, False, time)
        GObject.idle_add(self.refresh_page_list)
        # the index update will start a doc list refresh when finished
        GObject.idle_add(lambda: self.workers['index_updater'].start(
                         upd_docs=upd_docs, del_docs=del_docs, optimize=False))
