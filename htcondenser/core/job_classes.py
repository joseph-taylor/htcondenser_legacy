"""
Classes to describe jobs, groups of jobs, and other helper classes.
"""


import htcondenser.core.logging_config
import logging
import os
import re
from subprocess import check_call
from htcondenser.core.common import cp_hdfs, date_time_now
from collections import OrderedDict


log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class JobSet(object):
    """Manages a set of Jobs, all sharing a common submission file, log
    locations, resource request, and setup procedure.

    Parameters
    ----------
    exe : str
        Name of executable for this set of jobs. Note that path must be specified,
        e.g. './myexe'

    copy_exe : bool, optional
        If `True`, copies the executable to HDFS. Set `False` for builtins e.g. awk

    setup_script : str, optional
        Shell script to execute on worker node to setup necessary programs, libs, etc.

    filename : str, optional
        Filename for HTCondor job description file.

    out_dir : str, optional
        Directory for STDOUT output. Will be automatically created if it does not
        already exist. Raises an OSError if already exists but is not a directory.

    out_file : str, optional
        Filename for STDOUT output.

    err_dir : str, optional
        Directory for STDERR output. Will be automatically created if it does not
        already exist. Raises an OSError if already exists but is not a directory.

    err_file : str, optional
        Filename for STDERR output.

    log_dir : str, optional
        Directory for log output. Will be automatically created if it does not
        already exist. Raises an OSError if already exists but is not a directory.

    log_file : str, optional
        Filename for log output.

    cpus : int, optional
        Number of CPU cores for each job.

    memory : str, optional
        RAM to request for each job.

    disk : str, optional
        Disk space to request for each job.

    transfer_hdfs_input : bool, optional
        If True, transfers input files on HDFS to worker node first.
        Auto-updates program arguments to take this into account.
        Otherwise files are read directly from HDFS.
        Note that this does not affect input files **not** on HDFS - they will
        be transferred across regardlass.

    share_exe_setup: bool, optional
        If True, then all jobs will use the same exe and setup files on HDFS.
        If False, each job will have their own copy of the exe and setup script
        in their individual job folder.

    transfer_input_files : list[str], optional
        List of files to be transferred across for each job
        (from initial_dir for relative paths).
        **Usage of this argument is highly discouraged**
        (except in scenarios where you have a very small number of jobs,
        and the file(s) are very small) since it can lock up soolin due to both
        processor load and network load.
        Recommended to use input_files argument in Job() instead.

    transfer_output_files : list[str], optional
        List of files to be transferred across after each job
        (to initial_dir for relative paths).
        **Usage of this argument is highly discouraged**
        (except in scenarios where you have a very small number of jobs,
        and the file(s) are very small) since it can lock up soolin due to both
        processor load and network load.
        Recommended to use output_files argument in Job() instead.

    hdfs_store : str, optional
        If any local files (on `/user`) needs to be transferred to the job, it
        must first be stored on `/hdfs`. This argument specifies the directory
        where those files are stored. Each job will have its own copy of all
        input files, in a subdirectory with the Job name. If this directory does
        not exist, it will be created.

    dag_mode : bool, optional
        If False, writes all Jobs to submit file. If True, then the Jobs are
        part of a DAG and the submit file for this JobSet only needs a
        placeholder for jobs. Job arguments will be specified in the DAG file.

    other_args: dict, optional
        Dictionary of other job options to write to HTCondor submit file.
        These will be added in **before** any arguments or jobs.

    Raises
    ------
    OSError
        If any of `out_file`, `err_file`, or `log_file`, are blank or '.'.

    OSError
        If any of `out_dir`, `err_dir`, `log_dir`, `hdfs_store` cannot be created.

    """

    def __init__(self,
                 exe,
                 copy_exe=True,
                 setup_script=None,
                 filename='jobs.condor',
                 out_dir='logs', out_file='$(cluster).$(process).out',
                 err_dir='logs', err_file='$(cluster).$(process).err',
                 log_dir='logs', log_file='$(cluster).$(process).log',
                 cpus=1, memory='100MB', disk='100MB',
                 transfer_hdfs_input=True,
                 share_exe_setup=False,
                 transfer_input_files=None,
                 transfer_output_files=None,
                 hdfs_store=None,
                 dag_mode=False,
                 other_args=None):
        super(JobSet, self).__init__()
        self.exe = exe
        self.copy_exe = copy_exe
        self.setup_script = setup_script
        self.filename = filename
        self.out_dir = os.path.abspath(str(out_dir))
        self.out_file = str(out_file)
        self.err_dir = os.path.abspath(str(err_dir))
        self.err_file = str(err_file)
        self.log_dir = os.path.abspath(str(log_dir))
        self.log_file = str(log_file)
        self.cpus = int(cpus) if int(cpus) >= 1 else 1
        self.memory = str(memory)
        self.disk = str(disk)
        self.transfer_hdfs_input = transfer_hdfs_input
        self.share_exe_setup = share_exe_setup
        # can't use X[:] or [] idiom as [:] evaulated first (so breaks on None)
        if not transfer_output_files:
            transfer_input_files = []
        # need a copy, not a reference.
        self.transfer_input_files = transfer_input_files[:]
        if not transfer_output_files:
            transfer_output_files = []
        self.transfer_output_files = transfer_output_files[:]
        self.hdfs_store = hdfs_store
        self.dag_mode = dag_mode
        self.job_template = os.path.join(os.path.dirname(__file__), '../templates/job.condor')
        self.other_job_args = other_args

        # Hold all Job object this JobSet manages, key is Job name.
        self.jobs = OrderedDict()

        # Setup directories
        # ---------------------------------------------------------------------
        for d in [self.out_dir, self.err_dir, self.log_dir, self.hdfs_store]:
            if d and not os.path.isdir(d):
                log.info('Making directory %s', d)
                os.makedirs(d)

        # Check output filenames are not blank
        # ---------------------------------------------------------------------
        for f in [self.out_file, self.err_file, self.log_file]:
            bad_filenames = ['', '.']
            if f in bad_filenames:
                raise OSError('Bad output filename')

    def __eq__(self, other):
        return self.filename == other.filename

    def __getitem__(self, i):
        if isinstance(i, int):
            if i >= len(self):
                raise IndexError()
            return self.jobs.values()[i]
        elif isinstance(i, slice):
            return self.jobs.values()[i]
        else:
            raise TypeError('Invalid argument type - must be int or slice')

    def __len__(self):
        return len(self.jobs)

    def add_job(self, job):
        """Add a Job to the collection of jobs managed by this JobSet.

        Parameters
        ----------
        job: Job
            Job object to be added.

        Raises
        ------
        TypeError
            If `job` argument isn't of type Job (or derived type).

        KeyError
            If a job with that name is already governed by this JobSet object.
        """
        if not isinstance(job, Job):
            raise TypeError('Added job must by of type Job')

        if job.name in self.jobs:
            raise KeyError('Job %s already exists in JobSet' % job.name)

        self.jobs[job.name] = job
        job.manager = self

    def write(self, dag_mode):
        """Write jobs to HTCondor job file."""

        with open(self.job_template) as tfile:
            template = tfile.read()

        file_contents = self.generate_file_contents(template, dag_mode)

        log.info('Writing HTCondor job file to %s', self.filename)
        with open(self.filename, 'w') as jfile:
            jfile.write(file_contents)

    def generate_file_contents(self, template, dag_mode=False):
        """Create a job file contents from a template, replacing necessary fields
        and adding in all jobs with necessary arguments.

        Can either be used for normal jobs, in which case all jobs added, or
        for use in a DAG, where a placeholder for any job(s) is used.

        Parameters
        ----------
        template : str
            Job template as a single string, including tokens to be replaced.

        dag_mode : bool, optional
            If True, then submit file will only contain placeholder for job args.
            This is so it can be used in a DAG. Otherwise, the submit file will
            specify each Job attached to this JobSet.

        Returns
        -------
        str
            Completed job template.

        Raises
        ------
        IndexError
            If the JobSet has no Jobs attached.
        """

        if len(self.jobs) == 0:
            raise IndexError('You have not added any jobs to this JobSet.')

        worker_script = os.path.join(os.path.dirname(__file__),
                                     '../templates/condor_worker.py')

        if self.other_job_args:
            other_args_str = '\n'.join('%s = %s' % (str(k), str(v))
                                       for k, v in self.other_job_args.iteritems())
        else:
            other_args_str = None

        # Make replacements in template
        replacement_dict = {
            'EXE_WRAPPER': worker_script,
            'STDOUT': os.path.join(self.out_dir, self.out_file),
            'STDERR': os.path.join(self.err_dir, self.err_file),
            'STDLOG': os.path.join(self.log_dir, self.log_file),
            'CPUS': str(self.cpus),
            'MEMORY': self.memory,
            'DISK': self.disk,
            'TRANSFER_INPUT_FILES': ','.join(self.transfer_input_files),
            'TRANSFER_OUTPUT_FILES': ','.join(self.transfer_input_files),
            'OTHER_ARGS': other_args_str
        }

        for pattern, replacement in replacement_dict.iteritems():
            if replacement:
                template = template.replace("{%s}" % pattern, replacement)

        # Add jobs
        if dag_mode:
            # actual arguments are in the DAG file, only placeholders here
            template += 'arguments=$(%s)\n' % DAGMan.JOB_VAR_NAME
            template += 'queue\n'
        else:
            # specifiy each job in submit file
            for name, job in self.jobs.iteritems():
                template += '\n# %s\n' % name
                template += 'arguments="%s"\n' % job.generate_job_arg_str()
                template += '\nqueue %d\n' % job.quantity

        # Check we haven't left any unused tokens in the template.
        # If we have, then remove them.
        leftover_tokens = re.findall(r'{\w*}', template)
        if leftover_tokens:
            log.debug('Leftover tokens in job file:')
        for tok in leftover_tokens:
            log.debug('%s', tok)
            template = template.replace(tok, '')

        return template

    def transfer_to_hdfs(self):
        """Copy any necessary input files to HDFS.

        This transfers both common exe/setup (if self.share_exe_setup == True),
        and the individual files required by each Job.
        """
        # Do copying of exe/setup script here instead of through Jobs if only
        # 1 instance required on HDFS.
        if self.share_exe_setup:
            if self.copy_exe:
                log.info('Copying %s -->> %s', self.exe, self.hdfs_store)
                cp_hdfs(self.exe, self.hdfs_store)
            if self.setup_script:
                log.info('Copying %s -->> %s', self.setup_script, self.hdfs_store)
                cp_hdfs(self.setup_script, self.hdfs_store)

        # Get each job to transfer their necessary files
        for job in self.jobs.itervalues():
            job.transfer_to_hdfs()

    def submit(self):
        """Write HTCondor job file, copy necessary files to HDFS, and submit.
        Also prints out info for user.
        """
        self.write(dag_mode=False)

        self.transfer_to_hdfs()

        check_call(['condor_submit', self.filename])

        if self.log_dir == self.out_dir == self.err_dir:
            log.info('Output/error/htcondor logs written to %s', self.out_dir)
        else:
            for t, d in {'STDOUT': self.out_dir,
                         'STDERR': self.err_dir,
                         'HTCondor log': self.log_dir}:
                log.info('%s written to %s', t, d)


# this should prob be a dict or namedtuple
class FileMirror(object):
    """Simple class to store location of mirrored files: the original,
    the copy of HDFS, and the copy on the worker node."""
    def __init__(self, original, hdfs, worker):
        super(FileMirror, self).__init__()
        self.original = original
        self.hdfs = hdfs
        self.worker = worker

    def __repr__(self):
        arg_str = ', '.join(['%s=%s' % (k, v) for k, v in self.__dict__.iteritems()])
        return 'FileMirror(%s)' % (arg_str)

    def __str__(self):
        arg_str = ', '.join(['%s=%s' % (k, v) for k, v in self.__dict__.iteritems()])
        return 'FileMirror(%s)' % arg_str


class Job(object):
    """One job instance in a JobSet, with defined arguments and inputs/outputs.

    Parameters
    ----------
    name : str
        Name of this job. Must be unique in the managing JobSet, and DAGMan.

    args : list[str] or str, optional
        Arguments for this job.

    input_files : list[str], optional
        List of input files to be transferred across before running executable.
        If the path is not on HDFS, a copy will be placed on HDFS under
        `hdfs_store`/`job.name`. Otherwise, the original on HDFS will be used.

    output_files : list[str], optional
        List of output files to be transferred across to HDFS after executable finishes.
        If the path is on HDFS, then that will be the destination. Otherwise
        `hdfs_store`/`job.name` will be used as destination directory.

    quantity : int, optional
        Quantity of this Job to submit.

    hdfs_mirror_dir : str, optional
        Mirror directory for files to be put on HDFS. If not specified, will
        use `hdfs_mirror_dir`/self.name, where `hdfs_mirror_dir` is taken
        from the manager. If the directory does not exist, it is created.

    Raises
    ------
    KeyError
        If the user tries to create a Job in a JobSet which already manages
        a Job with that name.

    TypeError
        If the user tries to assign a manager that is not of type JobSet
        (or a derived class).
    """

    def __init__(self, name, args=None,
                 input_files=None, output_files=None,
                 quantity=1, hdfs_mirror_dir=None):
        super(Job, self).__init__()
        self._manager = None
        self.name = str(name)
        if not args:
            args = []
        self.args = args[:]
        if isinstance(args, str):
            self.args = args.split()
        if not input_files:
            input_files = []
        self.input_files = input_files[:]
        if not output_files:
            output_files = []
        self.output_files = output_files[:]
        self.quantity = int(quantity)
        # Hold settings for file mirroring on HDFS
        self.input_file_mirrors = []  # input original, mirror on HDFS, and worker
        self.output_file_mirrors = []  # output mirror on HDFS, and worker
        self.hdfs_mirror_dir = hdfs_mirror_dir

    def __eq__(self, other):
        return self.name == other.name

    @property
    def manager(self):
        """Returns the Job's managing JobSet."""
        return self._manager

    @manager.setter
    def manager(self, manager):
        """Set the manager for this Job.

        Also triggers the setting of other info that depends on having a manager,
        mainly setting up the file mirroring on HDFS for input and output files.
        """
        if not isinstance(manager, JobSet):
            raise TypeError('Incorrect object type set as Job manager - requires a JobSet object')
        self._manager = manager
        if manager.copy_exe:
            self.input_files.append(manager.exe)
        if manager.setup_script:
            self.input_files.append(manager.setup_script)
        # Setup mirroring in HDFS
        if not self.hdfs_mirror_dir:
            self.hdfs_mirror_dir = os.path.join(self.manager.hdfs_store, self.name)
            log.debug('Auto setting mirror dir %s', self.hdfs_mirror_dir)
        self.setup_input_file_mirrors(self.hdfs_mirror_dir)
        self.setup_output_file_mirrors(self.hdfs_mirror_dir)

    def setup_input_file_mirrors(self, hdfs_mirror_dir):
        """Attach a mirror HDFS location for each non-HDFS input file.
        Also attaches a location for the worker node, incase the user wishes to
        copy the input file from HDFS to worker node first before processing.

        Will correctly account for managing JobSet's preference for share_exe_setup.
        Since input_file_mirrors is used for generate_job_arg_str(), we need to add
        the exe/setup here, even though they don't get transferred by the Job itself.

        Parameters
        ----------
        hdfs_mirror_dir : str
            Location of directory to store mirrored copies.
        """
        for ifile in self.input_files:
            basename = os.path.basename(ifile)
            mirror_dir = hdfs_mirror_dir
            if (ifile in [self.manager.exe, self.manager.setup_script] and
                    self.manager.share_exe_setup):
                mirror_dir = self.manager.hdfs_store
            hdfs_mirror = (ifile if ifile.startswith('/hdfs')
                           else os.path.join(mirror_dir, basename))
            mirror = FileMirror(original=ifile, hdfs=hdfs_mirror, worker=basename)
            self.input_file_mirrors.append(mirror)

    def setup_output_file_mirrors(self, hdfs_mirror_dir):
        """Attach a mirror HDFS location for each output file.

        Parameters
        ----------
        hdfs_mirror_dir : str
            Location of directory to store mirrored copies.
        """
        for ofile in self.output_files:
            basename = os.path.basename(ofile)
            hdfs_mirror = (ofile if ofile.startswith('/hdfs')
                           else os.path.join(hdfs_mirror_dir, basename))
            mirror = FileMirror(original=ofile, hdfs=hdfs_mirror, worker=basename)
            self.output_file_mirrors.append(mirror)

    def transfer_to_hdfs(self):
        """Transfer files across to HDFS.

        Auto-creates HDFS mirror dir if it doesn't exist, but only if
        there are 1 or more files to transfer.

        Will not trasnfer exe or setup script if manager.share_exe_setup is True.
        That is left for the manager to do.
        """
        # skip the exe.setup script - the JobSet should handle this itself.
        files_to_transfer = []
        for ifile in self.input_file_mirrors:
            if ((ifile.original == ifile.hdfs) or (self.manager.share_exe_setup and
                    ifile.original in [self.manager.exe, self.manager.setup_script])):
                continue
            files_to_transfer.append(ifile)

        if len(files_to_transfer) > 0 and not os.path.isdir(self.hdfs_mirror_dir):
            os.makedirs(self.hdfs_mirror_dir)

        for ifile in files_to_transfer:
            log.info('Copying %s -->> %s', ifile.original, ifile.hdfs)
            cp_hdfs(ifile.original, ifile.hdfs)

    def generate_job_arg_str(self):
        """Generate arg string to pass to the condor_worker.py script.

        This includes the user's args (in `self.args`), but also includes options
        for input and output files, and automatically updating the args to
        account for new locations on HDFS or worker node.

        Returns
        -------
        str:
            Argument string for the job, to be passed to condor_worker.py

        """
        job_args = []
        if self.manager.setup_script:
            job_args.extend(['--setup', os.path.basename(self.manager.setup_script)])

        new_args = self.args[:]

        if self.manager.transfer_hdfs_input:
            # Replace input files in exe args with their worker node copies
            for ifile in self.input_file_mirrors:
                for i, arg in enumerate(new_args):
                    if arg == ifile.original:
                        new_args[i] = ifile.worker

                # Add input files to be transferred across
                job_args.extend(['--copyToLocal', ifile.hdfs, ifile.worker])
        else:
            # Replace input files in exe args with their HDFS node copies
            for ifile in self.input_file_mirrors:
                for i, arg in enumerate(new_args):
                    if arg == ifile.original:
                        new_args[i] = ifile.hdfs
                # Add input files to be transferred across,
                # but only if they originally aren't on hdfs
                if not ifile.original.startswith('/hdfs'):
                    job_args.extend(['--copyToLocal', ifile.hdfs, ifile.worker])

        log.debug("New job args:")
        log.debug(new_args)

        # Add output files to be transferred across
        # Replace output files in exe args with their worker node copies
        for ofile in self.output_file_mirrors:
            for i, arg in enumerate(new_args):
                if arg == ofile.original or arg == ofile.hdfs:
                    new_args[i] = ofile.worker
            job_args.extend(['--copyFromLocal', ofile.worker, ofile.hdfs])

        # Add the exe
        job_args.extend(['--exe', os.path.basename(self.manager.exe)])

        # Add arguments for exe MUST COME LAST AS GREEDY
        if new_args:
            job_args.append('--args')
            job_args.extend(new_args)

        # Convert everything to str
        job_args = [str(x) for x in job_args]
        return ' '.join(job_args)


class DAGMan(object):
    """Class to implement DAG, and manage Jobs and dependencies.

    Parameters
    ----------
    filename : str
        Filename to write DAG jobs.

    status_file : str, optional
        Filename for DAG status file. See
        https://research.cs.wisc.edu/htcondor/manual/current/2_10DAGMan_Applications.html#SECTION0031012000000000000000

    status_update_period : int or str, optional
        Refresh period for DAG status file in seconds.

    dot : str, optional
        Filename for dot file. dot can then be used to generate a pictoral
        representation of jobs in the DAG and their relationships.

    other_args : dict, optional
        Dictionary of {variable: value} for other DAG options.

    Attributes
    ----------
    JOB_VAR_NAME : str
        Name of variable to hold job arguments string to pass to condor_worker.py,
        required in both DAG file and condor submit file.
    """

    # name of variable for individual condor submit files
    JOB_VAR_NAME = 'jobOpts'

    def __init__(self,
                 filename='jobs.dag',
                 status_file='jobs.status',
                 status_update_period=30,
                 dot=None,
                 other_args=None):
        super(DAGMan, self).__init__()
        self.dag_filename = filename
        self.status_file = status_file
        self.status_update_period = str(status_update_period)
        self.dot = dot
        self.other_args = other_args

        # hold info about Jobs. key is name, value is a dict
        self.jobs = OrderedDict()

    def __getitem__(self, i):
        if isinstance(i, int):
            if i >= len(self):
                raise IndexError()
            return self.jobs.values()[i]['job']
        elif isinstance(i, slice):
            return [x['job'] for x in self.jobs.values()[i]]
        else:
            raise TypeError('Invalid argument type - must be int or slice')


    def __len__(self):
        return len(self.jobs)

    def add_job(self, job, requires=None, job_vars=None, retry=None):
        """Add a Job to the DAG.

        Parameters
        ----------
        job : Job
            Job object to be added to DAG

        requires : str, Job, iterable[str], iterable[Job], optional
            Individual or a collection of Jobs or job names that must run first
            before this job can run. i.e. the job(s) specified here are the
            parents, whilst the added job is their child.

        job_vars : str, optional
            String of job variables specifically for the DAG. Note that program
            arguments should be set in Job.args not here.

        retry : int or str, optional
            Number of retry attempts for this job. By default the job runs once,
            and if its exit code != 0, the job has failed.

        Raises
        ------
        KeyError
            If a Job with that name has already been added to the DAG.

        TypeError
            If the `job` argument is not of type Job.
            If `requires` argument is not of type str, Job, iterable(str)
            or iterable(Job).
        """
        if not isinstance(job, Job):
            raise TypeError('Cannot added a non-Job object to DAGMan.')

        if job.name in self.jobs:
            raise KeyError()

        # Append necessary job arguments to any user opts.
        job_vars = job_vars or ""
        job_vars += 'jobOpts="%s"' % job.generate_job_arg_str()

        self.jobs[job.name] = dict(job=job, job_vars=job_vars, retry=retry, requires=None)

        hierarchy_list = []
        # requires can be:
        # - a job name [str]
        # - a list/tuple/set of job names [list(str)]
        # - a Job [Job]
        # - a list/tuple/set of Jobs [list(Job)]
        if requires:
            if isinstance(requires, str):
                hierarchy_list.append(requires)
            elif isinstance(requires, Job):
                hierarchy_list.append(requires.name)
            elif hasattr(requires, '__getitem__'):  # maybe getattr better?
                for it in requires:
                    if isinstance(it, str):
                        hierarchy_list.append(it)
                    elif isinstance(it, Job):
                        hierarchy_list.append(it.name)
                    else:
                        raise TypeError('Can only add list of Jobs or list of job names')
            else:
                raise TypeError('Can only add Job(s) or job name(s)')

        # Keep list of names of Jobs that must be executed before this one.
        self.jobs[job.name]['requires'] = hierarchy_list

    def check_job_requirements(self, job):
        """Check that the required Jobs actually exist and have been added to DAG.

        Parameters
        ----------
        job : Job or str
            Job object or name of Job to check.

        Raises
        ------
        KeyError
            If job(s) have prerequisite jobs that have not been added to the DAG.

        TypeError
            If `job` argument is not of type str or Job, or an iterable of
            strings or Jobs.
        """
        job_name = ''
        if isinstance(job, Job):
            job_name = job.name
        elif isinstance(job, str):
            job_name = job
        else:
            log.debug(type(job))
            raise TypeError('job argument must be job name or Job object.')
        req_jobs = set(self.jobs[job_name]['requires'])
        all_jobs = set(self.jobs)
        if not req_jobs.issubset(all_jobs):
            raise KeyError('The following requirements on %s do not have corresponding '
                           'Job objects: %s' % (job_name, ', '.join(list(req_jobs - all_jobs))))

    def check_job_acyclic(self, job):
        """Check no circular requirements, e.g. A ->- B ->- A

        Get all requirements for all parent jobs recursively, and check for
        the presence of this job in that list.

        Parameters
        ----------
        job : Job or str
            Job or job name to check

        Raises
        ------
        RuntimeError
            If job has circular dependency.
        """
        job_name = job.name if isinstance(job, Job) else job
        parents = self.jobs[job_name]['requires']
        log.debug('Checking %s', job_name)
        log.debug(parents)
        while parents:
            new_parents = []
            for p in parents:
                grandparents = self.jobs[p]['requires']
                if job_name in grandparents:
                    raise RuntimeError("%s is in requirements for %s - cannot "
                                       "have cyclic dependencies" % (job_name, p))
                new_parents.extend(grandparents)
                parents = new_parents[:]
        return True

    def generate_job_str(self, job):
        """Generate a string for job, for use in DAG file.

        Includes condor job file, any vars, and other options e.g. RETRY.
        Job requirements (parents) are handled separately in another method.

        Parameters
        ----------
        job : Job or str
            Job or job name.

        Returns
        -------
        name : str
            Job listing for DAG file.

        Raises
        ------
        TypeError
            If `job` argument is not of type str or Job.
        """
        job_name = ''
        if isinstance(job, Job):
            job_name = job.name
        elif isinstance(job, str):
            job_name = job
        else:
            log.debug(type(job))
            raise TypeError('job argument must be job name or Job object.')

        job_obj = self.jobs[job_name]['job']
        job_contents = ['JOB %s %s' % (job_name, job_obj.manager.filename)]

        job_vars = self.jobs[job_name]['job_vars']
        if job_vars:
            job_contents.append('VARS %s %s' % (job_name, job_vars))

        job_retry = self.jobs[job_name]['retry']
        if job_retry:
            job_contents.append('RETRY %s %s' % (job_name, job_retry))

        return '\n'.join(job_contents)

    def generate_job_requirements_str(self, job):
        """Generate a string of prerequisite jobs for this job.

        Does a check to make sure that the prerequisite Jobs do exist in the DAG,
        and that DAG is acyclic.

        Parameters
        ----------
        job : Job or str
            Job object or name of job.

        Returns
        -------
        str
            Job requirements if prerequisite jobs. Otherwise blank string.

        Raises
        ------
        TypeError
            If `job` argument is not of type str or Job.
        """
        job_name = ''
        if isinstance(job, Job):
            job_name = job.name
        elif isinstance(job, str):
            job_name = job
        else:
            log.debug(type(job))
            raise TypeError('job argument must be job name or Job object.')

        self.check_job_requirements(job)
        self.check_job_acyclic(job)

        if self.jobs[job_name]['requires']:
            return 'PARENT %s CHILD %s' % (' '.join(self.jobs[job_name]['requires']), job_name)
        else:
            return ''

    def generate_dag_contents(self):
        """
        Generate DAG file contents as a string.

        Returns
        -------
        str:
            DAG file contents
        """
        # Hold each line as entry in this list, then finally join with \n
        contents = ['# DAG created at %s' % date_time_now(), '']

        # Add jobs
        for name in self.jobs:
            contents.append(self.generate_job_str(name))

        # Add parent-child relationships
        for name in self.jobs:
            req_str = self.generate_job_requirements_str(name)
            if req_str != '':
                contents.append(req_str)

        # Add other options for DAG
        if self.status_file:
            contents.append('')
            contents.append('NODE_STATUS_FILE %s %s' % (self.status_file, self.status_update_period))

        if self.dot:
            contents.append('')
            contents.append('# Make a visual representation of this DAG (for PDF format):')
            fmt = 'pdf'
            output_file = os.path.splitext(self.dot)[0] + '.' + fmt
            contents.append('# dot -T%s %s -o %s' % (fmt, self.dot, output_file))
            contents.append('DOT %s' % self.dot)

        if self.other_args:
            contents.append('')
            for k, v in self.other_args.iteritems():
                contents.append('%s = %s' % (k, v))

        contents.append('')
        return '\n'.join(contents)

    def get_managers(self):
        """Get a list of all unique JobSets managing Jobs in this DAG.

        Returns
        -------
        name : list
            List of unique JobSet objects.
        """
        return list(set([jdict['job'].manager for jdict in self.jobs.itervalues()]))

    def write(self):
        """Write DAG to file and causes all Jobs to write their HTCondor submit files."""
        dag_contents = self.generate_dag_contents()
        log.info('Writing DAG to %s', self.dag_filename)
        with open(self.dag_filename, 'w') as dfile:
            dfile.write(dag_contents)

        # Write job files for each JobSet
        for manager in self.get_managers():
            manager.write(dag_mode=True)

    def submit(self):
        """Write all necessary submit files, transfer files to HDFS, and submit DAG."""
        self.write()
        for manager in self.get_managers():
            manager.transfer_to_hdfs()
        check_call(['condor_submit_dag', self.dag_filename])
        log.info('Check DAG status:')
        log.info('DAGstatus.py %s', self.status_file)
