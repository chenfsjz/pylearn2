"""Generate and compile C modules for Python,
"""
import os, tempfile, StringIO, sys, logging, subprocess, cPickle, atexit, time, shutil, stat
import distutils.sysconfig

import numpy.distutils #TODO: TensorType should handle this
import theano

from theano.configparser import config
from theano.gof.cc import hash_from_code, hash_from_file
import compilelock # we will abuse the lockfile mechanism when reading and writing the registry

from theano.configparser import TheanoConfigParser, AddConfigVar, EnumStr, StrParam, IntParam, FloatParam, BoolParam
AddConfigVar('cmodule.mac_framework_link',
        "If set to true, breaks certain mac installations with the infamous Bus Error",
        BoolParam(False))

def local_bitwidth():
    """Return 32 for 32bit arch, 64 for 64bit arch"""
    # Note - it seems from an informal survey of machines at scipy2010
    # that platform.architecture is also a reliable way to get the bitwidth
    try:
        maxint = sys.maxint
    except AttributeError: # python 3 compatibility
        maxint = sys.maxsize
    return len('%x' % maxint) * 4

_logger=logging.getLogger("theano.gof.cmodule")
_logger.setLevel(logging.WARN)

def error(*args):
    _logger.error("ERROR: "+' '.join(str(a) for a in args))
def warning(*args):
    _logger.warning("WARNING: "+' '.join(str(a) for a in args))
def info(*args):
    _logger.info("INFO: "+' '.join(str(a) for a in args))
def debug(*args):
    _logger.debug("DEBUG: "+' '.join(str(a) for a in args))

METH_VARARGS="METH_VARARGS"
METH_NOARGS="METH_NOARGS"

def debug_counter(name, every=1):
    """Debug counter to know how often we go through some piece of code.

    This is a utility function one may use when debugging. Usage example:
        debug_counter('I want to know how often I run this line')
    """
    setattr(debug_counter, name, getattr(debug_counter, name, 0) + 1)
    n = getattr(debug_counter, name)
    if n % every == 0:
        print >>sys.stderr, "debug_counter [%s]: %s" % (name, n)

class ExtFunction(object):
    """A C function to put into a DynamicModule """

    name = ""
    """string - function's name"""

    code_block = ""
    """string - the entire code for the function.  Has the form ``static PyObject*
    <name>([...]){ ... }

    See Python's C API Reference for how to write c functions for python modules.
    """

    method = ""
    """str - calling method for this function (i.e. 'METH_VARARGS', 'METH_NOARGS')"""

    doc = ""
    """str - documentation string for this function"""

    def __init__(self, name, code_block, method, doc="undocumented"):
        self.name = name
        self.code_block = code_block
        self.method = method
        self.doc = doc

    def method_decl(self):
        """Returns the signature for this function that goes into the DynamicModule's method table"""
        return '\t{"%s", %s, %s, "%s"}' %(self.name, self.name, self.method, self.doc)

class DynamicModule(object):
    def __init__(self, name):
        self.name = name
        self.support_code = []
        self.functions = []
        self.includes = ["<Python.h>", "<iostream>"]
        self.includes.append('<numpy/arrayobject.h>') #TODO: this should come from TensorType
        self.init_blocks = ['import_array();'] #TODO: from TensorType

    def print_methoddef(self, stream):
        print >> stream, "static PyMethodDef MyMethods[] = {"
        for f in self.functions:
            print >> stream, f.method_decl(), ','
        print >> stream, "\t{NULL, NULL, 0, NULL}"
        print >> stream, "};"

    def print_init(self, stream):
        print >> stream, "PyMODINIT_FUNC init%s(void){" % self.name
        for b in self.init_blocks:
            print >> stream, '  ', b
        print >> stream, '  ', '(void) Py_InitModule("%s", MyMethods);' % self.name
        print >> stream, "}"


    def add_include(self, str):
        self.includes.append(str)
    def add_init_code(self, code):
        self.init_blocks.append(code)
    def add_support_code(self, code):
        if code not in self.support_code: #TODO: KLUDGE
            self.support_code.append(code)

    def add_function(self, fn):
        self.functions.append(fn)


    def code(self):
        sio = StringIO.StringIO()
        for inc in self.includes:
            if not inc:
                continue
            if inc[0] == '<' or inc[0] == '"':
                print >> sio, "#include", inc
            else:
                print >> sio, '#include "%s"'%inc

        print  >> sio, "//////////////////////"
        print  >> sio, "////  Support Code"
        print  >> sio, "//////////////////////"
        for sc in self.support_code:
            print >> sio, sc

        print  >> sio, "//////////////////////"
        print  >> sio, "////  Functions"
        print  >> sio, "//////////////////////"
        for f in self.functions:
            print >> sio, f.code_block

        print  >> sio, "//////////////////////"
        print  >> sio, "////  Module init"
        print  >> sio, "//////////////////////"
        self.print_methoddef(sio)
        self.print_init(sio)

        return sio.getvalue()

    def list_code(self, ofile=sys.stdout):
        """Print out the code with line numbers to `ofile` """
        for i, line in enumerate(self.code().split('\n')):
            print >> ofile, '%4i'%(i+1), line
        ofile.flush()

    #TODO: add_type

def dlimport(fullpath, suffix=None):
    """Dynamically load a .so, .pyd, .dll, or .py file

    :type fullpath: string
    :param fullpath: a fully-qualified path do a compiled python module
    :param suffix: a suffix to strip from the end of fullpath to get the import name
    :type suffix: string

    :returns: the dynamically loaded module (from __import__)

    """
    if not os.path.isabs(fullpath):
        raise ValueError('`fullpath` must be an absolute path', fullpath)
    if suffix is None:
        if fullpath.endswith('.so'):
            suffix = '.so'
        elif fullpath.endswith('.pyd'):
            suffix = '.pyd'
        elif fullpath.endswith('.dll'):
            suffix = '.dll'
        elif fullpath.endswith('.py'):
            suffix = '.py'
        else:
            suffix = ''
    rval = None
    if fullpath.endswith(suffix):
        module_name = '.'.join(fullpath.split(os.path.sep)[-2:])[:-len(suffix)]
    else:
        raise ValueError('path has wrong suffix', (fullpath, suffix))
    workdir = fullpath[:-len(module_name)- 1 - len(suffix)]

    debug("WORKDIR", workdir)
    debug("module_name", module_name)

    sys.path[0:0] = [workdir] #insert workdir at beginning (temporarily)
    try:
        rval = __import__(module_name, {}, {}, [module_name])
        if not rval:
            raise Exception('__import__ failed', fullpath)
    finally:
        del sys.path[0]

    assert fullpath.startswith(rval.__file__)
    return rval

def dlimport_workdir(basedir):
    """Return a directory where you should put your .so file for dlimport to be able to load
    it, given a basedir which should normally be config.compiledir"""
    return tempfile.mkdtemp(dir=basedir)

def last_access_time(path):
    """Return the number of seconds since the epoch of the last access of a given file"""
    return os.stat(path)[stat.ST_ATIME]

def module_name_from_dir(dirname):
    """Scan the contents of a cache directory and return full path of the dynamic lib in it.
    """
    files = os.listdir(dirname)
    name, = [file for file in files if file.endswith('.so') or file.endswith('.pyd')]
    return os.path.join(dirname, name)


def is_same_entry(entry_1, entry_2):
    """
    Return True iff both paths can be considered to point to the same module.

    This is the case if and only if at least one of these conditions holds:
        - They are equal.
        - There real paths are equal.
        - They share the same temporary work directory and module file name.
    """
    if entry_1 == entry_2:
        return True
    if os.path.realpath(entry_1) == os.path.realpath(entry_2):
        return True
    if (os.path.basename(entry_1) == os.path.basename(entry_2) and
        (os.path.basename(os.path.dirname(entry_1)) ==
         os.path.basename(os.path.dirname(entry_2))) and
        os.path.basename(os.path.dirname(entry_1).startswith('tmp'))):
        return True
    return False


def get_module_hash(src_code, key):

    """
    Return an MD5 hash that uniquely identifies a module.

    This hash takes into account:
        1. The C source code of the module (`src_code`).
        2. The version part of the key.
        3. The compiler options defined in `key` (command line parameters and
           libraries to link against).
    """
    # `to_hash` will contain any element such that we know for sure that if
    # it changes, then the module hash should be different.
    # We start with the source code itself (stripping blanks might avoid
    # recompiling after a basic indentation fix for instance).
    to_hash = map(str.strip, src_code.split('\n'))
    # Get the version part of the key (ignore if unversioned).
    if key[0]:
        to_hash += map(str, key[0])
    c_link_key = key[1]
    # Currently, in order to catch potential bugs early, we are very
    # convervative about the structure of the key and raise an exception
    # if it does not match exactly what we expect. In the future we may
    # modify this behavior to be less strict and be able to accomodate
    # changes to the key in an automatic way.
    error_msg = ("This should not happen unless someone modified the code "
                 "that defines the CLinker key, in which case you should "
                 "ensure this piece of code is still valid (and this "
                 "AssertionError may be removed or modified to accomodate "
                 "this change)")
    assert c_link_key[0] == 'CLinker.cmodule_key', error_msg
    for key_element in c_link_key[1:]:
        if isinstance(key_element, tuple):
            # This should be the C++ compilation command line parameters or the
            # libraries to link against.
            to_hash += list(key_element)
        elif isinstance(key_element, str):
            if key_element.startswith('md5:'):
                # This is the md5 hash of the config options. We can stop
                # here.
                break
            else:
                raise AssertionError(error_msg)
        else:
            raise AssertionError(error_msg)
    return hash_from_code('\n'.join(to_hash))


class KeyData(object):

    """Used to store the key information in the cache."""

    def __init__(self, keys, module_hash, key_pkl, entry):
        """
        Constructor.

        :param keys: Set of keys that are associated to the exact same module.

        :param module_hash: Hash identifying the module (it should hash both
        the code and the compilation options).

        :param key_pkl: Path to the file in which this KeyData object should be
        pickled.
        """
        self.keys = keys
        self.module_hash = module_hash
        self.key_pkl = key_pkl
        self.entry = entry

    def add_key(self, key, save_pkl=True):
        """Add a key to self.keys, and update pickled file if asked to."""
        assert key not in self.keys
        self.keys.add(key)
        if save_pkl:
            self.save_pkl()

    def remove_key(self, key, save_pkl=True):
        """Remove a key from self.keys, and update pickled file if asked to."""
        self.keys.remove(key)
        if save_pkl:
            self.save_pkl()

    def save_pkl(self):
        """
        Dump this object into its `key_pkl` file.

        May raise a cPickle.PicklingError if such an exception is raised at
        pickle time (in which case a warning is also displayed).
        """
        # Note that writing in binary mode is important under Windows.
        try:
            cPickle.dump(self, open(self.key_pkl, 'wb'),
                         protocol=cPickle.HIGHEST_PROTOCOL)
        except cPickle.PicklingError:
            warning("Cache leak due to unpickle-able key data", self.keys)
            os.remove(self.key_pkl)
            raise

    def get_entry(self):
        """Return path to the module file."""
        # TODO This method may be removed in the future (e.g. in 0.5) since
        # its only purpose is to make sure that old KeyData objects created
        # before the 'entry' field was added are properly handled.
        if not hasattr(self, 'entry'):
            self.entry = module_name_from_dir(os.path.dirname(self.key_pkl))
        return self.entry

    def delete_keys_from(self, entry_from_key, do_manual_check=True):
        """
        Delete from entry_from_key all keys associated to this KeyData object.

        Note that broken keys will not appear in the keys field, so we also
        manually look for keys associated to the same entry, unless
        do_manual_check is False.
        """
        entry = self.get_entry()
        for key in self.keys:
            del entry_from_key[key]
        if do_manual_check:
            to_del = []
            for key, key_entry in entry_from_key.iteritems():
                if key_entry == entry:
                    to_del.append(key)
            for key in to_del:
                del entry_from_key[key]


class ModuleCache(object):
    """Interface to the cache of dynamically compiled modules on disk

    Note that this interface does not assume exclusive use of the cache directory.
    It is built to handle the case where multiple programs are also using instances of this
    class to manage the same directory.

    The cache works on the basis of keys. Each key is mapped to only one
    dynamic module, but multiple keys may be mapped to the same module (see
    below for details).

    Keys should be tuples of length 2: (version, rest)
    The ``rest`` can be anything hashable and picklable, that uniquely identifies the
    computation in the module.

    The ``version`` should be a hierarchy of tuples of integers.
    If the ``version`` is either 0 or (), then the key is unversioned, and its
    corresponding module will be deleted in an atexit() handler if it is not
    associated to another versioned key.
    If the ``version`` is neither 0 nor (), then the module will be kept in the
    cache between processes.

    An unversioned module is not deleted by the process that creates it.  Deleting such modules
    does not work on NFS filesystems because the tmpdir in which the library resides is in use
    until the end of the process' lifetime.  Instead, unversioned modules are left in their
    tmpdirs without corresponding .pkl files.  These modules and their directories are erased
    by subsequent processes' refresh() functions.

    Two different keys are mapped to the same module when all conditions below
    are met:
        - They have the same version.
        - They share the same compilation options in their ``rest`` part (see
          ``CLinker.cmodule_key_`` for how this part is built).
        - They share the same C code.
    """

    dirname = ""
    """The working directory that is managed by this interface"""

    module_from_name = {}
    """maps a module filename to the loaded module object"""

    entry_from_key = {}
    """Maps keys to the filename of a .so/.pyd.
    """

    module_hash_to_key_data = {}
    """Maps hash of a module's code to its corresponding KeyData object."""

    stats = []
    """A list with counters for the number of hits, loads, compiles issued by module_from_key()
    """

    force_fresh = False
    """True -> Ignore previously-compiled modules
    """

    loaded_key_pkl = set()
    """set of all key.pkl files that have been loaded.
    """

    def __init__(self, dirname, force_fresh=None, check_for_broken_eq=True):
        """
        :param check_for_broken_eq: A bad __eq__ implemenation can break this cache mechanism.
        This option turns on a not-too-expensive sanity check during the load of an old cache
        file.
        """
        self.dirname = dirname
        self.module_from_name = dict(self.module_from_name)
        self.entry_from_key = dict(self.entry_from_key)
        self.module_hash_to_key_data = dict(self.module_hash_to_key_data)
        self.stats = [0, 0, 0]
        if force_fresh is not None:
            self.force_fresh = force_fresh
        self.loaded_key_pkl = set()

        self.refresh()

        start = time.time()
        if check_for_broken_eq:
            # Speed up comparison by only comparing keys for which the version
            # part is equal (since the version part is supposed to be made of
            # integer tuples, we can assume its hash is properly implemented).
            version_to_keys = {}
            for key in self.entry_from_key:
                version_to_keys.setdefault(key[0], []).append(key)
            for k0 in self.entry_from_key:
                # Compare `k0` to all keys `k1` with the same version part.
                for k1 in version_to_keys[k0[0]]:
                    if k0 is not k1 and k0 == k1:
                        warning(("The __eq__ and __hash__ functions are broken for some element"
                                " in the following two keys. The cache mechanism will say that"
                                " graphs like this need recompiling, when they could have been"
                                " retrieved:"))
                        warning("Key 0:", k0)
                        warning("Entry 0:", self.entry_from_key[k0])
                        warning("hash 0:", hash(k0))
                        warning("Key 1:", k1)
                        warning("Entry 1:", self.entry_from_key[k1])
                        warning("hash 1:", hash(k1))
        debug('Time needed to check broken equality / hash: %s' % (time.time() - start))

    age_thresh_use = 60*60*24*24
    """
    The default age threshold (in seconds) for cache files we want to use.

    Older modules will be deleted in ``clear_old``.
    """

    def refresh(self, delete_if_unpickle_failure=False):
        """Update self.entry_from_key by walking the cache directory structure.

        Add entries that are not in the entry_from_key dictionary.

        Remove entries which have been removed from the filesystem.

        Also, remove malformed cache directories.

        :param delete_if_unpickle_failure: If True, cache entries for which
        trying to unpickle the KeyData file fails with an unknown exception
        will be deleted.
        """
        start_time = time.time()
        too_old_to_use = []

        compilelock.get_lock()
        try:
            # add entries that are not in the entry_from_key dictionary
            time_now = time.time()
            for root, dirs, files in os.walk(self.dirname):
                key_pkl = os.path.join(root, 'key.pkl')
                if key_pkl in self.loaded_key_pkl:
                    continue
                elif 'delete.me' in files or not files:
                    # On NFS filesystems, it is impossible to delete a directory with open
                    # files in it.  So instead, some commands in this file will respond to a
                    # failed rmtree() by touching a 'delete.me' file.  This file is a message
                    # for a future process to try deleting the directory.
                    try:
                        _rmtree(root, ignore_nocleanup=True,
                                msg="delete.me found in dir")
                    except:
                        # Maybe directory is still in use? We just leave it
                        # for future removal (and make sure there is a
                        # delete.me file in it).
                        delete_me = os.path.join(root, 'delete.me')
                        if not os.path.exists(delete_me):
                            try:
                                open(delete_me, 'w')
                            except:
                                # Giving up!
                                warning("Cannot mark cache directory for "
                                        "deletion: %s" % root)
                elif 'key.pkl' in files:
                    try:
                        entry = module_name_from_dir(root)
                    except ValueError: # there is a key but no dll!
                        if not root.startswith("/tmp"):
                            # Under /tmp, file are removed periodically by the os.
                            # So it is normal that this happens from time to time.
                            warning("ModuleCache.refresh() Found key without dll in cache, deleting it.", key_pkl)
                        _rmtree(root, ignore_nocleanup=True,
                                msg="missing module file", level="info")
                        continue
                    if (time_now - last_access_time(entry)) < self.age_thresh_use:
                        debug('refresh adding', key_pkl)
                        def unpickle_failure():
                            info("ModuleCache.refresh() Failed to unpickle "
                                 "cache file", key_pkl)
                        try:
                            key_data = cPickle.load(open(key_pkl, 'rb'))
                        except EOFError:
                            # Happened once... not sure why (would be worth
                            # investigating if it ever happens again).
                            unpickle_failure()
                            _rmtree(root, ignore_nocleanup=True,
                                    msg='broken cache directory [EOF]',
                                    level='warning')
                            continue
                        except:
                            # TODO Note that in a development version, it may
                            # be better to raise exceptions instead of silently
                            # catching them.
                            unpickle_failure()
                            if delete_if_unpickle_failure:
                                _rmtree(root, ignore_nocleanup=True,
                                        msg='broken cache directory',
                                        level='info')
                            else:
                                # This exception is often triggered by keys that contain
                                # references to classes that have not yet been imported.  They are
                                # not necessarily broken.
                                pass
                            continue

                        if not isinstance(key_data, KeyData):
                            # This is some old cache data, that does not fit
                            # the new cache format. It would be possible to
                            # update it, but it is not entirely safe since we
                            # do not know the config options that were used.
                            # As a result, we delete it instead (which is also
                            # simpler to implement).
                            _rmtree(root, ignore_nocleanup=True,
                                    msg='deprecated cache entry')
                            continue

                        # Check the path to the module stored in the KeyData
                        # object matches the path to `entry`. There may be
                        # a mismatch e.g. due to symlinks, or some directory
                        # being renamed since last time cache was created.
                        kd_entry = key_data.get_entry()
                        if kd_entry != entry:
                            if is_same_entry(entry, kd_entry):
                                # Update KeyData object.
                                key_data.entry = entry
                            else:
                                # This is suspicious. Better get rid of it.
                                _rmtree(root, ignore_nocleanup=True,
                                        msg='module file path mismatch',
                                        level='info')
                                continue

                        # Find unversioned keys.
                        to_del = [key for key in key_data.keys if not key[0]]
                        if to_del:
                            warning("ModuleCache.refresh() Found unversioned "
                                    "key in cache, removing it.", key_pkl)
                            # Since the version is in the module hash, all
                            # keys should be unversioned.
                            if len(to_del) != len(key_data.keys):
                                warning('Found a mix of unversioned and '
                                        'versioned keys for the same '
                                        'module', key_pkl)
                            _rmtree(root, ignore_nocleanup=True,
                                    msg="unversioned key(s) in cache",
                                    level='info')
                            continue

                        for key in key_data.keys:
                            if key not in self.entry_from_key:
                                self.entry_from_key[key] = entry
                                # Assert that we have not already got this
                                # entry somehow.
                                assert entry not in self.module_from_name
                            else:
                                info("The same cache key is associated to "
                                     "different modules. This may happen "
                                     "if different processes compiled the "
                                     "same module independently. We will "
                                     "use %s instead of %s." %
                                     (self.entry_from_key[key], entry))
                        self.loaded_key_pkl.add(key_pkl)

                        # Remember the map from a module's hash to the KeyData
                        # object associated with it.
                        mod_hash = key_data.module_hash
                        if mod_hash in self.module_hash_to_key_data:
                            # This should not happen: a given module should
                            # never be duplicated in the cache.
                            info(
                                "Found duplicated modules in the cache! This "
                                "may happen if different processes compiled "
                                "the same module independently.")
                        else:
                            self.module_hash_to_key_data[mod_hash] = key_data
                    else:
                        too_old_to_use.append(entry)


            # Remove entries that are not in the filesystem.
            items_copy = list(self.module_hash_to_key_data.iteritems())
            for module_hash, key_data in items_copy:
                try:
                    # Test to see that the file is [present and] readable.
                    open(key_data.get_entry()).close()
                    gone = False
                except IOError:
                    gone = True
                if gone:
                    # Assert that we did not have one of the deleted files
                    # loaded up and in use.
                    # If so, it should not have been deleted. This should be
                    # considered a failure of the OTHER process, that deleted
                    # it.
                    if entry in self.module_from_name:
                        warning("A module that was loaded by this "
                                "ModuleCache can no longer be read from file "
                                "%s... this could lead to problems." % entry)
                        del self.module_from_name[entry]

                    info("deleting ModuleCache entry", entry)
                    key_data.delete_keys_from(self.entry_from_key)
                    del self.module_hash_to_key_data[module_hash]
                    if key[0]:
                        # this is a versioned entry, so should have been on disk
                        # Something weird happened to cause this, so we are responding by
                        # printing a warning, removing evidence that we ever saw this mystery
                        # key.
                        pkl_file_to_remove = key_data.key_pkl
                        if not root.startswith("/tmp"):
                            # Under /tmp, file are removed periodically by the os.
                            # So it is normal that this happen from time to time.
                            warning('Removing key file %s because the corresponding module is gone from the file system.' % pkl_file_to_remove)
                        self.loaded_key_pkl.remove(pkl_file_to_remove)

        finally:
            compilelock.release_lock()

        debug('Time needed to refresh cache: %s' % (time.time() - start_time))

        return too_old_to_use

    def module_from_key(self, key, fn=None, keep_lock=False, key_data=None):
        """
        :param fn: A callable object that will return an iterable object when
        called, such that the first element in this iterable object is the
        source code of the module, and the last element is the module itself.
        `fn` is called only if the key is not already in the cache, with
        a single keyword argument `location` that is the path to the directory
        where the module should be compiled.

        :param key_data: If not None, it should be a KeyData object and the
        key parameter should be None. In this case, we use the info from the
        KeyData object to recover the module, rather than the key itself. Note
        that this implies the module already exists (and may or may not have
        already been loaded).
        """
        # We should only use one of the two ways to get a module.
        assert key_data is None or key is None
        rval = None
        if key is not None:
            try:
                _version, _rest = key
            except:
                raise ValueError(
                        "Invalid key. key must have form (version, rest)", key)
        name = None
        if key is not None and key in self.entry_from_key:
            # We have seen this key either in this process or previously.
            name = self.entry_from_key[key]
        elif key_data is not None:
            name = key_data.get_entry()
        if name is not None:
            # This is an existing module we can recover.
            if name not in self.module_from_name:
                debug('loading name', name)
                self.module_from_name[name] = dlimport(name)
                self.stats[1] += 1
            else:
                self.stats[0] += 1
            debug('returning compiled module from cache', name)
            rval = self.module_from_name[name]
        else:
            hash_key = hash(key)
            key_data = None
            # We have never seen this key before.
            # Acquire lock before creating things in the compile cache,
            # to avoid that other processes remove the compile dir while it
            # is still empty.
            compilelock.get_lock()
            # This try/finally block ensures that the lock is released once we
            # are done writing in the cache file or after raising an exception.
            try:
                location = dlimport_workdir(self.dirname)
            except OSError, e:
                error(e)
                if e.errno == 31:
                    error('There are', len(os.listdir(config.compiledir)),
                            'files in', config.compiledir)
                raise

            try:
                compile_steps = fn(location=location).__iter__()

                # Check if we already know a module with the same hash. If we
                # do, then there is no need to even compile it.
                duplicated_module = False
                # The first compilation step is to yield the source code.
                src_code = compile_steps.next()
                module_hash = get_module_hash(src_code, key)
                if module_hash in self.module_hash_to_key_data:
                    debug("Duplicated module! Will re-use the previous one")
                    duplicated_module = True
                    # Load the already existing module.
                    key_data = self.module_hash_to_key_data[module_hash]
                    # Note that we do not pass the `fn` argument, since it
                    # should not be used considering that the module should
                    # already be compiled.
                    module = self.module_from_key(key=None, key_data=key_data)
                    name = module.__file__
                    # Add current key to the set of keys associated to the same
                    # module. We only save the KeyData object of versioned
                    # modules.
                    try:
                        key_data.add_key(key, save_pkl=bool(_version))
                    except cPickle.PicklingError:
                        # This should only happen if we tried to save the
                        # pickled file.
                        assert _version
                        # The key we are trying to add is broken: we will not
                        # add it after all.
                        key_data.remove_key(key)
                        key_broken = True

                    # We can delete the work directory.
                    _rmtree(location, ignore_nocleanup=True)

                else:
                    # Will fail if there is an error compiling the C code.
                    # The exception will be caught and the work dir will be
                    # deleted.
                    while True:
                        try:
                            # The module should be returned by the last
                            # step of the compilation.
                            module = compile_steps.next()
                        except StopIteration:
                            break

                    # Obtain path to the '.so' module file.
                    name = module.__file__

                    debug("Adding module to cache", key, name)
                    assert name.startswith(location)
                    assert name not in self.module_from_name
                    # Changing the hash of the key is not allowed during
                    # compilation. That is the only cause found that makes the
                    # following assert fail.
                    assert hash(key) == hash_key
                    assert key not in self.entry_from_key

                    key_pkl = os.path.join(location, 'key.pkl')
                    assert not os.path.exists(key_pkl)
                    key_data = KeyData(
                            keys=set([key]),
                            module_hash=module_hash,
                            key_pkl=key_pkl,
                            entry=name)

                    if _version: # save the key
                        try:
                            key_data.save_pkl()
                            key_broken = False
                        except cPickle.PicklingError:
                            key_broken = True
                            # Remove key from the KeyData object, to make sure
                            # we never try to save it again.
                            # We still keep the KeyData object and save it so
                            # that the module can be re-used in the future.
                            key_data.keys = set()
                            key_data.save_pkl()

                        # TODO We should probably have a similar sanity check
                        # when we add a new key to an existing KeyData object,
                        # not just when we create a brand new one.
                        if not key_broken:
                            try:
                                kd2 = cPickle.load(open(key_pkl, 'rb'))
                                assert len(kd2.keys) == 1
                                key_from_file = kd2.keys.__iter__().next()
                                if key != key_from_file:
                                    raise Exception(
                                        "Key not equal to unpickled version "
                                        "(Hint: verify the __eq__ and "
                                        "__hash__ functions for your Ops",
                                        (key, key_from_file))
                            except cPickle.UnpicklingError:
                                warning('Cache failure due to un-loadable key',
                                        key)

                        # Adding the KeyData file to this set means it is a
                        # versioned module.
                        self.loaded_key_pkl.add(key_pkl)

                    # Map the new module to its KeyData object. Note that we
                    # need to do it regardless of whether the key is versioned
                    # or not if we want to be able to re-use this module inside
                    # the same process.
                    self.module_hash_to_key_data[module_hash] = key_data

            except:
                # TODO try / except / finally is not Python2.4-friendly.
                # This may happen e.g. when an Op has no C implementation. In
                # any case, we do not want to keep around the temporary work
                # directory, as it may cause trouble if we create too many of
                # these. The 'ignore_if_missing' flag is set just in case this
                # directory would have already been deleted.
                _rmtree(location, ignore_if_missing=True)
                raise

            finally:
                # Release lock if needed.
                if not keep_lock:
                    compilelock.release_lock()

            # Update map from key to module name for all keys associated to
            # this same module.
            all_keys = key_data.keys
            if not all_keys:
                # Should only happen for broken keys.
                assert key_broken
                all_keys = [key]
            else:
                assert key in key_data.keys
            for k in all_keys:
                if k in self.entry_from_key:
                    # If we had already seen this key, then it should be
                    # associated to the same module.
                    assert self.entry_from_key[k] == name
                else:
                    self.entry_from_key[k] = name

            if name in self.module_from_name:
                # May happen if we are re-using an existing module.
                assert duplicated_module
                assert self.module_from_name[name] is module
            else:
                self.module_from_name[name] = module

            self.stats[2] += 1
            rval = module
        #debug('stats', self.stats, sum(self.stats))
        return rval

    age_thresh_del = 60*60*24*31#31 days
    age_thresh_del_unversioned = 60*60*24*7#7 days

    """The default age threshold for `clear_old` (in seconds)
    """
    def clear_old(self, age_thresh_del=None, delete_if_unpickle_failure=False):
        """
        Delete entries from the filesystem for cache entries that are too old.

        :param age_thresh_del: Dynamic modules whose last access time is more
        than ``age_thresh_del`` seconds ago will be erased. Defaults to 31-day
        age if not provided.

        :param delete_if_unpickle_failure: See help of refresh() method.
        """
        if age_thresh_del is None:
            age_thresh_del = self.age_thresh_del

        compilelock.get_lock()
        try:
            # Update the age of modules that have been accessed by other
            # processes and get all module that are too old to use
            # (not loaded in self.entry_from_key).
            too_old_to_use = self.refresh(
                    delete_if_unpickle_failure=delete_if_unpickle_failure)
            time_now = time.time()

            # Build list of module files and associated keys.
            entry_to_key_data = dict((entry, None) for entry in too_old_to_use)
            for key_data in self.module_hash_to_key_data.itervalues():
                entry = key_data.get_entry()
                # Since we loaded this file, it should not be in
                # too_old_to_use.
                assert entry not in entry_to_key_data
                entry_to_key_data[entry] = key_data

            for entry, key_data in entry_to_key_data.iteritems():
                age = time_now - last_access_time(entry)
                if age > age_thresh_del:
                    # TODO: we are assuming that modules that haven't been accessed in over
                    # age_thresh_del are not currently in use by other processes, but that could be
                    # false for long-running jobs...
                    assert entry not in self.module_from_name
                    if key_data is not None:
                        key_data.delete_keys_from(self.entry_from_key)
                        del self.module_hash_to_key_data[key_data.module_hash]
                        if key_data.key_pkl in self.loaded_key_pkl:
                            self.loaded_key_pkl.remove(key_data.key_pkl)
                    parent = os.path.dirname(entry)
                    assert parent.startswith(os.path.join(self.dirname, 'tmp'))
                    _rmtree(parent, msg='old cache directory', level='info')

        finally:
            compilelock.release_lock()

    def clear(self, unversioned_min_age=None, clear_base_files=False,
              delete_if_unpickle_failure=False):
        """
        Clear all elements in the cache.

        :param unversioned_min_age: Forwarded to `clear_unversioned`. In
        particular, you can set it to -1 in order to delete all unversioned
        cached modules regardless of their age.

        :param clear_base_files: If True, then delete base directories
        'cuda_ndarray' and 'cutils_ext' if they are present. If False, those
        directories are left intact.

        :param delete_if_unpickle_failure: See help of refresh() method.
        """
        compilelock.get_lock()
        try:
            self.clear_old(
                    age_thresh_del=-1.0,
                    delete_if_unpickle_failure=delete_if_unpickle_failure)
            self.clear_unversioned(min_age=unversioned_min_age)
            if clear_base_files:
                self.clear_base_files()
        finally:
            compilelock.release_lock()

    def clear_base_files(self):
        """
        Delete base directories 'cuda_ndarray' and 'cutils_ext' if present.
        """
        compilelock.get_lock()
        try:
            for base_dir in ('cuda_ndarray', 'cutils_ext'):
                to_delete = os.path.join(self.dirname, base_dir)
                if os.path.isdir(to_delete):
                    try:
                        shutil.rmtree(to_delete)
                        debug('Deleted: %s' % to_delete)
                    except:
                        warning('Could not delete %s' % to_delete)
        finally:
            compilelock.release_lock()

    def clear_unversioned(self, min_age=None):
        """
        Delete unversioned dynamic modules.

        They are deleted both from the internal dictionaries and from the
        filesystem.

        :param min_age: Minimum age to be deleted, in seconds. Defaults to
        7-day age if not provided.
        """
        if min_age is None:
            min_age = self.age_thresh_del_unversioned

        compilelock.get_lock()
        all_key_datas = self.module_hash_to_key_data.values()
        try:
            for key_data in all_key_datas:
                if not key_data.keys:
                    # May happen for broken versioned keys.
                    continue
                for key_idx, key in enumerate(key_data.keys):
                    version, rest = key
                    if version:
                        # Since the version is included in the module hash,
                        # it should not be possible to mix versioned and
                        # unversioned keys in the same KeyData object.
                        assert key_idx == 0
                        break
                if not version:
                    # Note that unversioned keys cannot be broken, so we can
                    # set do_manual_check to False to speed things up.
                    key_data.delete_keys_from(self.entry_from_key,
                                              do_manual_check=False)
                    entry = key_data.get_entry()
                    # Entry is guaranteed to be in this dictionary, because
                    # an unversioned entry should never have been loaded via
                    # refresh.
                    assert entry in self.module_from_name

                    del self.module_from_name[entry]
                    del self.module_hash_to_key_data[key_data.module_hash]

                    parent = os.path.dirname(entry)
                    assert parent.startswith(os.path.join(self.dirname, 'tmp'))
                    _rmtree(parent, msg='unversioned', level='info')

            # Sanity check: all unversioned keys should have been removed at
            # this point.
            for key in self.entry_from_key:
                assert key[0]

            time_now = time.time()
            for filename in os.listdir(self.dirname):
                if filename.startswith('tmp'):
                    try:
                        open(os.path.join(self.dirname, filename, 'key.pkl')).close()
                        has_key = True
                    except IOError:
                        has_key = False
                    if not has_key:
                        age = time_now - last_access_time(os.path.join(self.dirname, filename))
                        # In normal case, the processus that created this directory
                        # will delete it. However, if this processus crashes, it
                        # will not be cleaned up.
                        # As we don't know if this directory is still used, we wait
                        # one week and suppose that the processus crashed, and we
                        # take care of the clean-up.
                        if age > min_age:
                            _rmtree(os.path.join(self.dirname, filename),
                                    msg='old unversioned', level='info')
        finally:
            compilelock.release_lock()

    def _on_atexit(self):
        # Note: no need to call refresh() since it is called by clear_old().
        compilelock.get_lock()
        try:
            self.clear_old()
            self.clear_unversioned()
        finally:
            compilelock.release_lock()

def _rmtree(parent, ignore_nocleanup=False, msg='', level='debug',
            ignore_if_missing=False):
    if ignore_if_missing and not os.path.exists(parent):
        return
    try:
        if ignore_nocleanup or not config.nocleanup:
            log_msg = 'Deleting'
            if msg:
                log_msg += ' (%s)' % msg
            eval(level)('%s: %s' % (log_msg, parent))
            shutil.rmtree(parent)
    except Exception, e:
        # If parent still exists, mark it for deletion by a future refresh()
        if os.path.exists(parent):
            try:
                info('placing "delete.me" in', parent)
                open(os.path.join(parent,'delete.me'), 'w').close()
            except Exception, ee:
                warning('Failed to remove or mark cache directory %s for removal' % parent, ee)

_module_cache = None
def get_module_cache(dirname, force_fresh=None):
    global _module_cache
    if _module_cache is None:
        _module_cache = ModuleCache(dirname, force_fresh=force_fresh)
        atexit.register(_module_cache._on_atexit)
    if _module_cache.dirname != dirname:
        warning("Returning module cache instance with different dirname than you requested")
    return _module_cache

def get_lib_extension():
    """Return the platform-dependent extension for compiled modules."""
    if sys.platform == 'win32':
        return 'pyd'
    else:
        return 'so'

def get_gcc_shared_library_arg():
    """Return the platform-dependent GCC argument for shared libraries."""
    if sys.platform == 'darwin':
        return '-dynamiclib'
    else:
        return '-shared'

def std_include_dirs():
    return numpy.distutils.misc_util.get_numpy_include_dirs() + [distutils.sysconfig.get_python_inc()]

def std_lib_dirs_and_libs():
    python_inc = distutils.sysconfig.get_python_inc()
    if sys.platform == 'win32':
        # Typical include directory: C:\Python26\include
        libname = os.path.basename(os.path.dirname(python_inc)).lower()
        # Also add directory containing the Python library to the library
        # directories.
        python_lib_dir = os.path.join(os.path.dirname(python_inc), 'libs')
        lib_dirs = [python_lib_dir]
        return [libname], [python_lib_dir]
    #DSE Patch 2 for supporting OSX frameworks. Suppress -lpython2.x when frameworks are present
    elif sys.platform=='darwin' :
        if python_inc.count('Python.framework') :
            return [],[]
        else :
            libname=os.path.basename(python_inc)
            return [libname],[]
    else:
        # Typical include directory: /usr/include/python2.6
        libname = os.path.basename(python_inc)
        return [libname],[]

def std_libs():
    return std_lib_dirs_and_libs()[0]

def std_lib_dirs():
    return std_lib_dirs_and_libs()[1]

p=subprocess.Popen(['gcc','-dumpversion'],stdout=subprocess.PIPE)
p.wait()
gcc_version_str = p.stdout.readline().strip()
del p

def gcc_version():
    return gcc_version_str

def gcc_module_compile_str(module_name, src_code, location=None, include_dirs=[], lib_dirs=[], libs=[],
        preargs=[]):
    """
    :param module_name: string (this has been embedded in the src_code
    :param src_code: a complete c or c++ source listing for the module
    :param location: a pre-existing filesystem directory where the cpp file and .so will be written
    :param include_dirs: a list of include directory names (each gets prefixed with -I)
    :param lib_dirs: a list of library search path directory names (each gets prefixed with -L)
    :param libs: a list of libraries to link with (each gets prefixed with -l)
    :param preargs: a list of extra compiler arguments

    :returns: dynamically-imported python module of the compiled code.
    """
    #TODO: Do not do the dlimport in this function

    if preargs is None:
        preargs = []
    else:
        preargs = list(preargs)

    if sys.platform != 'win32':
        # Under Windows it looks like fPIC is useless. Compiler warning:
        # '-fPIC ignored for target (all code is position independent)'
        preargs.append('-fPIC')
    no_opt = False

    include_dirs = include_dirs + std_include_dirs()
    libs = std_libs() + libs
    lib_dirs = std_lib_dirs() + lib_dirs
    if sys.platform == 'win32':
        python_inc = distutils.sysconfig.get_python_inc()
        # Typical include directory: C:\Python26\include
        libname = os.path.basename(os.path.dirname(python_inc)).lower()
        # Also add directory containing the Python library to the library
        # directories.
        python_lib_dir = os.path.join(os.path.dirname(python_inc), 'libs')
        lib_dirs = [python_lib_dir] + lib_dirs
    else:
        # Typical include directory: /usr/include/python2.6
        python_inc = distutils.sysconfig.get_python_inc()
        libname = os.path.basename(python_inc)

    #DSE Patch 1 for supporting OSX frameworks; add -framework Python
    if sys.platform=='darwin' :
        preargs.extend(['-undefined','dynamic_lookup'])
        # link with the framework library *if specifically requested*
        # config.mac_framework_link is by default False, since on some mac
        # installs linking with -framework causes a Bus Error
        if python_inc.count('Python.framework')>0 and config.cmodule.mac_framework_link:
            preargs.extend(['-framework','Python'])

        # Figure out whether the current Python executable is 32 or 64 bit and compile accordingly
        n_bits = local_bitwidth()
        preargs.extend(['-m%s' % n_bits])
        debug("OS X: compiling for %s bit architecture" % n_bits)

    # sometimes, the linker cannot find -lpython so we need to tell it
    # explicitly where it is located
    # this returns somepath/lib/python2.x
    python_lib = distutils.sysconfig.get_python_lib(plat_specific=1, \
                    standard_lib=1)
    python_lib = os.path.dirname(python_lib)
    if python_lib not in lib_dirs:
        lib_dirs.append(python_lib)

    workdir = location

    cppfilename = os.path.join(location, 'mod.cpp')
    cppfile = file(cppfilename, 'w')

    debug('Writing module C++ code to', cppfilename)
    ofiles = []
    rval = None

    cppfile.write(src_code)
    # Avoid gcc warning "no newline at end of file".
    if not src_code.endswith('\n'):
        cppfile.write('\n')
    cppfile.close()

    lib_filename = os.path.join(location, '%s.%s' %
            (module_name, get_lib_extension()))

    debug('Generating shared lib', lib_filename)
    cmd = ['g++', get_gcc_shared_library_arg(), '-g']
    if no_opt:
        cmd.extend(p for p in preargs if not p.startswith('-O'))
    else:
        cmd.extend(preargs)
    cxxflags = [flag for flag in config.gcc.cxxflags.split(' ') if flag]
    #print >> sys.stderr, config.gcc.cxxflags.split(' ')
    cmd.extend(cxxflags)
    cmd.extend('-I%s'%idir for idir in include_dirs)
    cmd.extend(['-o',lib_filename])
    cmd.append(cppfilename)
    cmd.extend(['-L%s'%ldir for ldir in lib_dirs])
    cmd.extend(['-l%s'%l for l in libs])
    #print >> sys.stderr, 'COMPILING W CMD', cmd
    debug('Running cmd', ' '.join(cmd))

    p = subprocess.Popen(cmd)
    status = p.wait()

    if status:
        print '==============================='
        for i, l in enumerate(src_code.split('\n')):
            #gcc put its messages to stderr, so we add ours now
            print >> sys.stderr, '%05i\t%s'%(i+1, l)
        print '==============================='
        print >> sys.stderr, "command line:",' '.join(cmd)
        raise Exception('g++ return status', status)

    #touch the __init__ file
    file(os.path.join(location, "__init__.py"),'w').close()
    return dlimport(lib_filename)

def icc_module_compile_str(*args):
    raise NotImplementedError()
