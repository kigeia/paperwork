#    Paperwork - Using OCR to grep dead trees the easy way
#    Copyright (C) 2012  Jerome Flesch
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

import os
import sys
import logging
import threading
import time
import traceback

from gi.repository import GObject

logger = logging.getLogger(__name__)


class _WorkerThread(object):
    def __init__(self):
        self.__must_stop = False
        self._todo = []
        self._running = None
        self._sem = threading.Semaphore(0)
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        logger.debug("Workers: Worker thread started")

        while True:
            self._running = None
            self._sem.acquire()
            self._running = self._todo.pop()
            (worker, kwargs) = self._running
            if worker is None:
                logger.debug("Workers: Worker thread halting")
                return
            try:
                worker._wrapper(**kwargs)
            except Exception, exc:
                logger.exception("Worker [%s] raised an exception:"
                        % worker.name)

        logger.debug("Workers: Worker thread stopped")

    def queue_worker(self, worker, kwargs):
        logger.debug("Workers: Queueing [%s]" % (worker.name))
        self._todo.append((worker, kwargs))
        self._sem.release()

    def halt(self):
        logger.debug("Workers: Requesting halt")
        self._todo.append((None, None))
        self._sem.release()


_WORKER_THREAD = _WorkerThread()


def halt():
    _WORKER_THREAD.halt()


class BasicWorker(GObject.GObject):
    can_interrupt = False
    can_pause = False

    def __init__(self, name):
        GObject.GObject.__init__(self)
        self.name = name
        self.can_run = True
        self.paused = False
        self.is_running = False
        self.__started_by = None

    def do(self, **kwargs):
        # implemented by the child class
        #
        # if can_interrupt = True, the child class must check self.can_run as
        # often as possible
        #
        # if can_pause = True, the child class must check self.paused as
        # often as possible. do() must return if self.paused = True.
        # do() will be called again with resume=<value it returned>
        assert()

    def _wrapper(self, **kwargs):
        if not self.can_run:
            return
        self.is_running = True
        if "resume" in kwargs:
            logger.debug("Workers: [%s] resumed" % (self.name))
        else:
            logger.debug("Workers: [%s] started" % (self.name))
        try:
            return self.do(**kwargs)
        finally:
            self.is_running = False
            self.__started_by = None
            if self.paused:
                logger.debug("Workers: [%s] paused" % (self.name))
            else:
                logger.debug("Workers: [%s] ended" % (self.name))

    def start(self, **kwargs):
        if self.is_running and self.can_run:
            logger.error("=====")
            logger.error("ERROR")
            logger.error("Thread '%s' was already started by:" % (self.name))
            idx = 0
            for stack_el in self.__started_by:
                logger.error("%2d: %20s: L%5d: %s"
                       % (idx, os.path.basename(stack_el[0]),
                          stack_el[1], stack_el[2]))
                idx += 1
            logger.error("=====")
            raise threading.ThreadError(
                ("Tried to start a thread already running: %s"
                 % (self.name)))
        self.__started_by = traceback.extract_stack()
        self.can_run = True

    def soft_stop(self):
        logger.debug("Stopping worker [%s]" % (self))
        if not self.can_interrupt and self.is_running:
            logger.warn("Trying to stop worker [%s], but it cannot be stopped"
                   % (self.name))
        self.can_run = False


class Worker(BasicWorker):
    def __init__(self, name):
        BasicWorker.__init__(self, name)
        self.__is_in_queue = False
        self.__must_restart = False
        self.__last_args = {}
        self.__last_ret_value = None
        self.__lock = threading.Lock()

    def _wrapper(self, **kwargs):
        self.__lock.acquire()
        try:
            if not self.can_run:
                return
            self.__last_ret_value = BasicWorker._wrapper(self, **kwargs)
        finally:
            self.__lock.release()
            if self.__must_restart:
                _WORKER_THREAD.queue_worker(self, self.__last_args)
                self.__must_restart = False
            else:
                self.__is_in_queue = False

    def __pause_all(self):
        global _WORKER_THREAD
        todo = _WORKER_THREAD._todo[:]
        others = [worker for (worker, kwargs) in todo]
        running = _WORKER_THREAD._running
        if running is not None:
            others.append(running[0])
        paused = []
        for other in others:
            if other == self:
                continue
            if not other.can_pause:
                continue
            other.pause()
            paused.append(other)
        return paused

    def __resume(self, workers):
        for worker in workers:
            worker.resume()

    def start(self, **kwargs):
        global _WORKER_THREAD
        BasicWorker.start(self)
        self.__last_args = kwargs
        paused = self.__pause_all()
        try:
            logger.debug("Worker %s: %d ; %s" % (self.name, self.__is_in_queue,
                                          self.is_running))
            if not self.__is_in_queue:
                _WORKER_THREAD.queue_worker(self, kwargs)
                self.__is_in_queue = True
            else:
                self.__must_restart = True
        finally:
            self.__resume(paused)

    def pause(self):
        assert(self.can_pause)
        if not self.__is_in_queue:
            return
        self.paused = True
        self.wait()

    def resume(self):
        if not self.paused:
            return
        self.wait()
        self.paused = False
        args = self.__last_args.copy()
        if not self.__last_ret_value is None:
            args['resume'] = self.__last_ret_value
        _WORKER_THREAD.queue_worker(self, args)
        self.__is_in_queue = True

    def stop(self):
        self.soft_stop()
        self.wait()

    def wait(self):
        self.__lock.acquire()
        self.__lock.release()

    def __str__(self):
        return self.name


class IndependentWorker(BasicWorker):
    def __init__(self, name):
        BasicWorker.__init__(self, name)
        self.thread = None

    def start(self, **kwargs):
        BasicWorker.start(self)

        self.thread = threading.Thread(target=self._wrapper, kwargs=kwargs)
        self.thread.start()

    def stop(self):
        logger.debug("Stopping worker [%s]" % (self))
        self.soft_stop()
        self.wait()

    def wait(self):
        if not self.is_running:
            return

        if self.thread is not None and self.thread.is_alive():
            self.thread.join()
            assert(not self.is_running)

    def __str__(self):
        return self.name


class IndependentWorkerQueue(IndependentWorker):
    can_interrupt = True

    __gsignals__ = {
        'queue-start': (GObject.SignalFlags.RUN_LAST, None, ()),
        'queue-stop': (GObject.SignalFlags.RUN_LAST, None,
                       # Arg: Exception raised by a worker, None if none
                       (GObject.TYPE_PYOBJECT, )),
    }
    local_signals = ['queue-start', 'queue-stop']

    def __init__(self, name):
        IndependentWorker.__init__(self, name)
        self.__queue = []
        self.__current_worker = None
        self.__signals = {}

    def add_worker(self, worker):
        for (signal, (handler, kargs)) in self.__signals.iteritems():
            worker.connect(signal, handler, *kargs)
        self.__queue.append(worker)

    def do(self, **kwargs):
        self.emit('queue-start')
        exception = None
        try:
            try:
                while len(self.__queue) > 0 and self.can_run:
                    self.__current_worker = self.__queue.pop(0)
                    logger.debug("Queue [%s]: Starting worker [%s]"
                           % (self.name, self.__current_worker.name))
                    self.__current_worker.do(**kwargs)
                    logger.debug("Queue [%s]: Worker [%s] has ended"
                           % (self.name, self.__current_worker.name))
            except Exception, exc:
                exception = exc
                raise
        finally:
            self.__current_worker = None
            self.emit('queue-stop', exception)

    def connect(self, signal, handler, *kargs):
        if signal in self.local_signals:
            Worker.connect(self, signal, handler, *kargs)
            return
        self.__signals[signal] = (handler, kargs)
        for worker in self.__queue:
            worker.connect(signal, handler, *kargs)

    def stop(self):
        if self.__current_worker is not None:
            self.__current_worker.stop()
        Worker.stop(self)

GObject.type_register(IndependentWorkerQueue)


class WorkerProgressUpdater(IndependentWorker):
    """
    Update a progress bar a predefined timing.
    """

    can_interrupt = True

    NB_UPDATES = 50

    def __init__(self, name, progressbar):
        self.name = "Progress bar updater: %s" % (name)
        IndependentWorker.__init__(self, self.name)
        self.progressbar = progressbar

    def do(self, value_min=0.0, value_max=0.5, total_time=20.0):
        for upd in range(0, self.NB_UPDATES):
            if not self.can_run:
                return
            val = value_max - value_min
            val *= upd
            val /= self.NB_UPDATES
            val += value_min

            GObject.idle_add(self.progressbar.set_fraction, val)
            time.sleep(total_time / self.NB_UPDATES)


GObject.type_register(WorkerProgressUpdater)
