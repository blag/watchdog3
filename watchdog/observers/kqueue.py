# -*- coding: utf-8 -*-
# kqueue.py: kqueue-based event emitter.
#
# Copyright (C) 2010 Gora Khargosh <gora.khargosh@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
:module: watchdog.observers.kqueue
:synopsis: ``kqueue(2)`` based emitter implementation.
:author: Gora Khargosh <gora.khargosh@gmail.com>
:platforms: Mac OS X and BSD with kqueue(2).

.. NOTE:: About ``select.kqueue`` and Python versions.

    * Python 2.5 does not ship with ``select.kqueue``
    * Python 2.6 ships with a broken ``select.kqueue`` that cannot take
      multiple events in the event list passed to ``kqueue.control``.
    * Python 2.7 ships with a working ``select.kqueue``
      implementation.

    I have backported the Python 2.7 implementation to Python 2.5 and 2.6
    in the ``select_backport`` package available on PyPI.

Classes
-------
.. autoclass:: KqueueEmitter
   :members:
   :show-inheritance:

Collections and Utility Classes
-------------------------------
.. autoclass:: KeventDescriptor
   :members:
   :show-inheritance:

.. autoclass:: KeventDescriptorSet
   :members:
   :show-inheritance:

"""


from __future__ import with_statement

from watchdog.utils import has_attribute, platform, absolute_path, \
    real_absolute_path

if platform.is_bsd() or platform.is_darwin():
    import threading
    import errno
    import os
    import os.path
    import sys
    import stat

    import select
    if not has_attribute(select, 'kqueue') or sys.version < (2, 7, 0):
        import select_backport as select

    from watchdog.utils.dirsnapshot import DirectorySnapshot
    from watchdog.observers.api import EventEmitter, DEFAULT_EMITTER_TIMEOUT
    from watchdog.events import \
        DirMovedEvent, \
        DirDeletedEvent, \
        DirCreatedEvent, \
        DirModifiedEvent, \
        FileMovedEvent, \
        FileDeletedEvent, \
        FileCreatedEvent, \
        FileModifiedEvent, \
        EVENT_TYPE_MOVED, \
        EVENT_TYPE_DELETED

    # Maximum number of events to process.
    MAX_EVENTS = 4096

    # Mac OS X file system performance guidelines:
    # --------------------------------------------
    # http://developer.apple.com/library/ios/#documentation/Performance/Conceptual/FileSystem/Articles/TrackingChanges.html#//apple_ref/doc/uid/20001993-CJBJFIDD
    # http://www.mlsite.net/blog/?p=2312
    #
    # Specifically:
    # -------------
    # When you only want to track changes on a file or directory, be sure to
    # open it# using the O_EVTONLY flag. This flag prevents the file or
    # directory from being marked as open or in use. This is important
    # if you are tracking files on a removable volume and the user tries to
    # unmount the volume. With this flag in place, the system knows it can
    # dismiss the volume. If you had opened the files or directories without
    # this flag, the volume would be marked as busy and would not be unmounted.
    O_EVTONLY = 0x8000

    # Pre-calculated values for the kevent filter, flags, and fflags attributes.
    if platform.is_darwin():
        WATCHDOG_OS_OPEN_FLAGS = O_EVTONLY
    else:
        WATCHDOG_OS_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK
    WATCHDOG_KQ_FILTER = select.KQ_FILTER_VNODE
    WATCHDOG_KQ_EV_FLAGS = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
    WATCHDOG_KQ_FFLAGS = \
        select.KQ_NOTE_DELETE | \
        select.KQ_NOTE_WRITE | \
        select.KQ_NOTE_EXTEND | \
        select.KQ_NOTE_ATTRIB | \
        select.KQ_NOTE_LINK | \
        select.KQ_NOTE_RENAME | \
        select.KQ_NOTE_REVOKE


    # Flag tests.
    def is_deleted(kev):
        """Determines whether the given kevent represents deletion."""
        return kev.fflags & select.KQ_NOTE_DELETE

    def is_modified(kev):
        """Determines whether the given kevent represents modification."""
        fflags = kev.fflags
        return (fflags & select.KQ_NOTE_EXTEND) or (fflags & select.KQ_NOTE_WRITE)

    def is_attrib_modified(kev):
        """Determines whether the given kevent represents attribute modification."""
        return kev.fflags & select.KQ_NOTE_ATTRIB

    def is_renamed(kev):
        """Determines whether the given kevent represents movement."""
        return kev.fflags & select.KQ_NOTE_RENAME


    class KeventDescriptorSet(object):
        """
        Thread-safe kevent descriptor collection.
        """
        def __init__(self):
            # Set of KeventDescriptor
            self._descriptors = set()

            # Descriptor for a given path.
            self._descriptor_for_path = dict()

            # Descriptor for a given fd.
            self._descriptor_for_fd = dict()

            # List of kevent objects.
            self._kevents = set()

            self._lock = threading.Lock()


        @property
        def kevents(self):
            """List of kevents monitored."""
            with self._lock:
                return list(self._kevents)

        @property
        def paths(self):
            """List of paths for which kevents have been created."""
            with self._lock:
                return self._descriptor_for_path.keys()

        def get_for_fd(self, fd):
            """
            Given a file descriptor, returns the kevent descriptor object
            for it.

            :param fd:
                OS file descriptor.
            :type fd:
                ``int``
            :returns:
                A :class:`KeventDescriptor` object.
            """
            with self._lock:
                return self._descriptor_for_fd[fd]

        def get(self, path):
            """
            Obtains a :class:`KeventDescriptor` object for the specified path.

            :param path:
                Path for which the descriptor will be obtained.
            """
            return self.__getitem__(path)

        def __getitem__(self, path):
            """
            Obtains a :class:`KeventDescriptor` object for the specified path.

            :param path:
                Path for which the descriptor will be obtained.
            """
            with self._lock:
                path = absolute_path(path)
                return self._descriptor_for_path[path]

        def __contains__(self, path):
            """
            Determines whether a :class:`KeventDescriptor has been registered
            for the specified path.

            :param path:
                Path for which the descriptor will be obtained.
            """
            with self._lock:
                path = absolute_path(path)
                return self._has_path(path)

        def add(self, path, is_directory):
            """
            Adds a :class:`KeventDescriptor` to the collection for the given
            path.

            :param path:
                The path for which a :class:`KeventDescriptor` object will be
                added.
            :param is_directory:
                ``True`` if the path refers to a directory; ``False`` otherwise.
            :type is_directory:
                ``bool``
            """
            with self._lock:
                path = absolute_path(path)
                if not self._has_path(path):
                    descriptor = KeventDescriptor(path, is_directory)
                    self._add_descriptor(descriptor)

        def remove(self, path):
            """
            Removes the :class:`KeventDescriptor` object for the given path
            if it already exists.

            :param path:
                Path for which the :class:`KeventDescriptor` object will be
                removed.
            """
            with self._lock:
                path = absolute_path(path)
                if self._has_path(path):
                    self._remove_descriptor(self[path])

        def clear(self):
            """
            Clears the collection and closes all open descriptors.
            """
            with self._lock:
                for descriptor in self._descriptors:
                    descriptor.close()
                self._descriptors.clear()
                self._descriptor_for_fd.clear()
                self._descriptor_for_path.clear()
                self._kevents.clear()

        # Thread-unsafe methods. Locking is provided at a higher level.
        def _has_path(self, path):
            """Determines whether a :class:`KeventDescriptor` for the specified
            path exists already in the collection."""
            return path in self._descriptor_for_path

        def _add_descriptor(self, descriptor):
            """
            Adds a descriptor to the collection.

            :param descriptor:
                An instance of :class:`KeventDescriptor` to be added.
            """
            self._descriptors.add(descriptor)
            self._kevents.add(descriptor.kevent)
            self._descriptor_for_path[descriptor.path] = descriptor
            self._descriptor_for_fd[descriptor.fd] = descriptor

        def _remove_descriptor(self, descriptor):
            """
            Removes a descriptor from the collection.

            :param descriptor:
                An instance of :class:`KeventDescriptor` to be removed.
            """
            self._descriptors.remove(descriptor)
            del self._descriptor_for_fd[descriptor.fd]
            del self._descriptor_for_path[descriptor.path]
            self._kevents.remove(descriptor.kevent)
            descriptor.close()


    class KeventDescriptor(object):
        """
        A kevent descriptor convenience data structure to keep together:

            * kevent
            * directory status
            * path
            * file descriptor

        :param path:
            Path string for which a kevent descriptor will be created.
        :param is_directory:
            ``True`` if the path refers to a directory; ``False`` otherwise.
        :type is_directory:
            ``bool``
        """
        def __init__(self, path, is_directory):
            self._path = absolute_path(path)
            self._kev = kev
            self._is_directory = is_directory
            self._fd = os.open(path, WATCHDOG_OS_OPEN_FLAGS)
            self._kev = select.kevent(self._fd,
                                      filter=WATCHDOG_KQ_FILTER,
                                      flags=WATCHDOG_KQ_EV_FLAGS,
                                      fflags=WATCHDOG_KQ_FFLAGS)

        @property
        def fd(self):
            """OS file descriptor for the kevent descriptor."""
            return self._fd

        @property
        def path(self):
            """The path associated with the kevent descriptor."""
            return self._path

        @property
        def kevent(self):
            """The kevent object associated with the kevent descriptor."""
            return self._kev

        @property
        def is_directory(self):
            """Determines whether the kevent descriptor refers to a directory.

            :returns:
                ``True`` or ``False``
            """
            return self._is_directory

        def close(self):
            """
            Closes the file descriptor associated with a kevent descriptor.
            """
            try:
                os.close(self.fd)
            except OSError, e:
                pass

        def _key(self):
            return (self.path, self.is_directory)

        def __eq__(self, descriptor):
            return self._key() == descriptor._key()

        def __ne__(self, descriptor):
            return self._key() != descriptor._key()

        def __hash__(self):
            return hash(self._key())


    class KqueueEmitter(EventEmitter):
        """
        kqueue(2)-based event emitter.

        .. NOTE:: About ``kqueue(2)`` and this implementation.

                  ``kqueue(2)`` monitors file system events only for
                  open descriptors, which means, this emitter does a lot of
                  book-keeping behind the scenes to keep track of open
                  descriptors for every entry in the monitored directory tree.

                  This also means the number of maximum open file descriptors
                  on your system must be increased **manually**.
                  Usually, issuing a call to ``ulimit`` should suffice::

                      ulimit -n 1024

                  Ensure that you pick a number that is larger than the
                  number of files you expect to be monitored.

                  ``kqueue(2)`` does not provide enough information about the
                  following things:

                  * The destination path of a file or directory that is renamed.
                  * Creation of a file or directory within a directory; in this
                    case, ``kqueue(2)`` only indicates a modified event on the
                    parent directory.

                  Therefore, this emitter takes a snapshot of the directory
                  tree when ``kqueue(2)`` detects a change on the file system
                  to be able to determine the above information.

        :param event_queue:
            The event queue to fill with events.
        :param watch:
            A watch object representing the directory to monitor.
        :type watch:
            :class:`watchdog.observers.api.ObservedWatch`
        :param timeout:
            Read events blocking timeout (in seconds).
        :type timeout:
            ``float``
        """
        def __init__(self, event_queue, watch, timeout=DEFAULT_EMITTER_TIMEOUT):
            EventEmitter.__init__(self, event_queue, watch, timeout)

            self._kq = select.kqueue()
            self._lock = threading.RLock()

            # A collection of KeventDescriptor.
            self._descriptors = KeventDescriptorSet()

            def walker_callback(path, stat_info, self=self):
                self._register_kevent(path, stat.S_ISDIR(stat_info.st_mode))
            self._snapshot = DirectorySnapshot(watch.path,
                                               watch.is_recursive,
                                               walker_callback)


        def _register_kevent(self, path, is_directory):
            """
            Registers a kevent descriptor for the given path.

            :param path:
                Path for which a kevent descriptor will be created.
            :param is_directory:
                ``True`` if the path refers to a directory; ``False`` otherwise.
            :type is_directory:
                ``bool``
            """
            try:
                self._descriptors.add(path, is_directory)
            except OSError, e:
                if e.errno == errno.ENOENT:
                    # Probably dealing with a temporary file that was created
                    # and then quickly deleted before we could open
                    # a descriptor for it. Therefore, simply queue a sequence
                    # of created and deleted events for the path.
                    path = absolute_path(path)
                    if is_directory:
                        self.queue_event(DirCreatedEvent(path))
                        self.queue_event(DirDeletedEvent(path))
                    else:
                        self.queue_event(FileCreatedEvent(path))
                        self.queue_event(FileDeletedEvent(path))
                else:
                    # All other errors are propagated.
                    raise

        def _unregister_kevent(self, path):
            """
            Convenience function to close the kevent descriptor for a
            specified kqueue-monitored path.

            :param path:
                Path for which the kevent descriptor will be closed.
            """
            self._descriptors.remove(path)

        def queue_event(self, event):
            """
            Handles queueing a single event object.

            :param event:
                An instance of :class:`watchdog.events.FileSystemEvent`
                or a subclass.
            """
            # Handles all the book keeping for queued events.
            # We do not need to fire moved/deleted events for all subitems in
            # a directory tree here, because this function is called by kqueue
            # for all those events anyway.
            with self._lock:
                if event.event_type == EVENT_TYPE_MOVED:
                    self._unregister_kevent(event.src_path)
                    self._register_kevent(event.dest_path, event.is_directory)
                elif event.event_type == EVENT_TYPE_DELETED:
                    self._unregister_kevent(event.src_path)
                EventEmitter.queue_event(self, event)


        def _queue_dirs_modified(self,
                                 dirs_modified,
                                 ref_snapshot,
                                 new_snapshot):
            pass


        def _queue_events_except_renames_and_dir_modifications(self, event_list):
            """
            Queues events from the kevent list returned from the call to
            :meth:`select.kqueue.control`.

            .. NOTE:: Queues only the deletions, file modifications,
                      attribute modifications. The other events, namely,
                      file creation, directory modification, file rename,
                      directory rename, directory creation, etc. are
                      determined by comparing directory snapshots.
            """
            paths_renamed = set()
            dirs_modified = set()

            for kev in event_list:
                descriptor = self._descriptors.get_for_fd(kev.ident)
                src_path = descriptor.path

                if is_deleted(kev):
                    if descriptor.is_directory:
                        self.queue_event(DirDeletedEvent(src_path))
                    else:
                        self.queue_event(FileDeletedEvent(src_path))
                elif is_attrib_modified(kev):
                    if descriptor.is_directory:
                        self.queue_event(DirModifiedEvent(src_path))
                    else:
                        self.queue_event(FileModifiedEvent(src_path))
                elif is_modified(kev):
                    if descriptor.is_directory:
                        # When a directory is modified, it may be due to
                        # sub-file/directory renames or new file/directory
                        # creation. We determine all this by comparing
                        # snapshots later.
                        dirs_modified.add(src_path)
                    else:
                        self.queue_event(FileModifiedEvent(src_path))
                elif is_renamed(kev):
                    # Kqueue does not specify the destination names for renames
                    # to, so we have to process these after taking a snapshot
                    # of the directory.
                    paths_renamed.add(src_path)
            return paths_renamed, dirs_modified


        def _queue_renamed(self, src_path, is_directory, ref_snapshot, new_snapshot):
            """
            Compares information from two directory snapshots (one taken before
            the rename operation and another taken right after) to determine the
            destination path of the file system object renamed, and adds
            appropriate events to the event queue.
            """
            try:
                ref_stat_info = ref_snapshot.stat_info(src_path)
            except KeyError:
                # Probably caught a temporary file/directory that was renamed
                # and deleted. Fires a sequence of created and deleted events
                # for the path.
                if is_directory:
                    self.queue_event(DirCreatedEvent(src_path))
                    self.queue_event(DirDeletedEvent(src_path))
                else:
                    self.queue_event(FileCreatedEvent(src_path))
                    self.queue_event(FileDeletedEvent(src_path))
                # We don't process any further and bail out assuming
                # the event represents deletion/creation instead of movement.
                return

            try:
                dest_path = absolute_path(new_snapshot.path_for_inode(ref_stat_info.st_ino))
                if is_directory:
                    event = DirMovedEvent(src_path, dest_path)
                    # TODO: Do we need to fire moved events for the items
                    # inside the directory tree? Does kqueue does this
                    # all by itself? Check this and then enable this code
                    # only if it doesn't already.
                    #if self.watch.is_recursive:
                    #    for sub_event in event.sub_moved_events():
                    #        self.queue_event(sub_event)
                else:
                    event = FileMovedEvent(src_path, dest_path)
                self.queue_event(event)
            except KeyError:
                # If the new snapshot does not have an inode for the
                # old path, we haven't found the new name. Therefore,
                # we mark it as deleted and remove unregister the path.
                if is_directory:
                    self.queue_event(DirDeletedEvent(src_path))
                else:
                    self.queue_event(FileDeletedEvent(src_path))


        def _read_events(self, timeout=None):
            """
            Reads events from a call to the blocking
            :meth:`select.kqueue.control()` method.

            :param timeout:
                Blocking timeout for reading events.
            :type timeout:
                ``float`` (seconds)
            """
            return self._kq.control(self._descriptors.kevents(), max_events=MAX_EVENTS, timeout=timeout)


        def queue_events(self, timeout):
            """
            Queues events by reading them from a call to the blocking
            :meth:`select.kqueue.control()` method.

            :param timeout:
                Blocking timeout for reading events.
            :type timeout:
                ``float`` (seconds)
            """
            with self._lock:
                try:
                    event_list = self._read_events(timeout=timeout)
                    paths_renamed, dir_modified = \
                        self._queue_events_except_renames_and_dir_modifications(event_list)

                    # Take a fresh snapshot of the directory and update the
                    # saved snapshot.
                    new_snapshot = DirectorySnapshot(watch.path, watch.is_recursive)
                    ref_snapshot = self._snapshot
                    self._snapshot = new_snapshot

                    if paths_renamed or dirs_modified:
                        for src_path in paths_renamed:
                            self._queue_renamed(src_path, is_directory, ref_snapshot, new_snapshot)
                        self._queue_from_dirs_modified(ref_snapshot,
                                                       new_snapshot,
                                                       dirs_modified)
                except OSerror, e:
                    if e.errno == errno.EBADF:
                        #logging.debug(e)
                        pass
                    else:
                        raise

        def on_thread_exit(self):
            # Clean up.
            with self._lock:
                self._descriptors.clear()
                self._kq.close()