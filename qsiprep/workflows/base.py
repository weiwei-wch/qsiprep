#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
qsiprep base processing workflows
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. autofunction:: init_qsiprep_wf
.. autofunction:: init_single_subject_wf

"""
import logging
import sys
import os
from copy import deepcopy

from nipype import __version__ as nipype_ver
from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu

from nilearn import __version__ as nilearn_ver

from ..engine import Workflow
from ..interfaces import (BIDSDataGrabber, BIDSInfo, BIDSFreeSurferDir,
                          SubjectSummary, AboutSummary, DerivativesDataSink)
from ..utils.bids import collect_data
from ..utils.misc import fix_multi_T1w_source_name
from ..utils.grouping import group_dwi_scans
from ..__about__ import __version__

from .anatomical import init_anat_preproc_wf
from .dwi.base import init_dwi_preproc_wf
from .dwi.finalize import init_dwi_finalize_wf
from .dwi.intramodal_template import init_intramodal_template_wf
from .dwi.util import get_source_file


LOGGER = logging.getLogger('nipype.workflow')


def init_qsiprep_wf(
        subject_list, run_uuid, work_dir, output_dir, bids_dir, ignore, debug, low_mem, anat_only,
        longitudinal, b0_threshold, hires, denoise_before_combining, dwi_denoise_window,
        unringing_method, dwi_no_biascorr, no_b0_harmonization, output_resolution,
        combine_all_dwis, omp_nthreads, force_spatial_normalization, skull_strip_template,
        skull_strip_fixed_seed, freesurfer, hmc_model, impute_slice_threshold, hmc_transform,
        shoreline_iters, eddy_config, write_local_bvecs, output_spaces, template, motion_corr_to,
        b0_to_t1w_transform, intramodal_template_iters, intramodal_template_transform,
        prefer_dedicated_fmaps, fmap_bspline, fmap_demean, use_syn, force_syn):
    """
    This workflow organizes the execution of qsiprep, with a sub-workflow for
    each subject.

    .. workflow::
        :graph2use: orig
        :simple_form: yes

        import os
        os.environ['FREESURFER_HOME'] = os.getcwd()
        from qsiprep.workflows.base import init_qsiprep_wf
        wf = init_qsiprep_wf(subject_list=['qsipreptest'],
                              run_uuid='X',
                              work_dir='.',
                              output_dir='.',
                              bids_dir='.',
                              ignore=[],
                              debug=False,
                              low_mem=False,
                              anat_only=False,
                              longitudinal=False,
                              b0_threshold=100,
                              freesurfer=False,
                              hires=False,
                              denoise_before_combining=True,
                              dwi_denoise_window=7,
                              unringing_method='mrdegibbs',
                              dwi_no_biascorr=False,
                              no_b0_harmonization=False,
                              combine_all_dwis=True,
                              omp_nthreads=1,
                              output_resolution=2.0,
                              hmc_model='3dSHORE',
                              skull_strip_template='OASIS',
                              skull_strip_fixed_seed=False,
                              output_spaces=['T1w', 'template'],
                              template='MNI152NLin2009cAsym',
                              motion_corr_to='iterative',
                              b0_to_t1w_transform='Rigid',
                              intramodal_template_iters=0,
                              intramodal_template_transform="Rigid",
                              hmc_transform='Affine',
                              eddy_config=None,
                              shoreline_iters=2,
                              impute_slice_threshold=0,
                              write_local_bvecs=False,
                              prefer_dedicated_fmaps=False,
                              fmap_bspline=False,
                              fmap_demean=True,
                              use_syn=True,
                              force_spatial_normalization=True,
                              force_syn=True)


    Parameters:

        subject_list : list
            List of subject labels
        run_uuid : str
            Unique identifier for execution instance
        work_dir : str
            Directory in which to store workflow execution state and temporary
            files
        output_dir : str
            Directory in which to save derivatives
        bids_dir : str
            Root directory of BIDS dataset
        ignore : list
            Preprocessing steps to skip (may include "slicetiming",
            "fieldmaps")
        low_mem : bool
            Write uncompressed .nii files in some cases to reduce memory usage
        anat_only : bool
            Disable diffusion workflows
        longitudinal : bool
            Treat multiple sessions as longitudinal (may increase runtime)
            See sub-workflows for specific differences
        b0_threshold : int
            Images with b-values less than this value will be treated as a b=0 image.
        dwi_denoise_window : int
            window size in voxels for ``dwidenoise``. Must be odd. If 0, '
            '``dwidwenoise`` will not be run'
        unringing_method : str
            algorithm to use for removing Gibbs ringing. Options: none, mrdegibbs
        dwi_no_biascorr : bool
            run spatial bias correction (N4) on dwi series
        no_b0_harmonization : bool
            skip rescaling dwi scans to have matching b=0 intensities across scans
        denoise_before_combining : bool
            'run ``dwidenoise`` before combining dwis. Requires ``combine_all_dwis``'
        combine_all_dwis : bool
            Combine all dwi sequences within a session into a single data set
        omp_nthreads : int
            Maximum number of threads an individual process may use
        skull_strip_template : str
            Name of ANTs skull-stripping template ('OASIS' or 'NKI')
        skull_strip_fixed_seed : bool
            Do not use a random seed for skull-stripping - will ensure
            run-to-run replicability when used with --omp-nthreads 1
        freesurfer : bool
            Enable FreeSurfer surface reconstruction (may increase runtime)
        hires : bool
            Enable sub-millimeter preprocessing in FreeSurfer
        output_spaces : list
            List of output spaces functional images are to be resampled to.
            Some parts of pipeline will only be instantiated for some output
            spaces.
        template : str
            Name of template targeted by ``template`` output space
        motion_corr_to : str
            Motion correct using the 'first' b0 image or use an 'iterative'
            method to motion correct to the midpoint of the b0 images
        b0_to_t1w_transform : "Rigid" or "Affine"
            Use a rigid or full affine transform for b0-T1w registration
        intramodal_template_iters: int
            Number of iterations for finding the midpoint image from the b0 templates
            from all groups. Has no effect if there is only one group. If 0, all b0
            templates are directly registered to the t1w image.
        intramodal_template_transform: str
            Transformation used for building the intramodal template.
        hmc_model : 'none', '3dSHORE' or 'MAPMRI'
            Model used to generate target images for head motion correction. If 'none'
            the transform from the nearest b0 will be used.
        hmc_transform : "Rigid" or "Affine"
            Type of transform used for head motion correction
        impute_slice_threshold : float
            Impute data in slices that are this many SDs from expected. If 0, no slices
            will be imputed.
        eddy_config: str
            Path to a JSON file containing config options for eddy
        prefer_dedicated_fmaps: bool
            If a reverse PE fieldmap is available in fmap, use that even if a reverse PE
            DWI series is available
        write_local_bvecs : bool
            Write out a series of voxelwise bvecs
        fmap_bspline : bool
            **Experimental**: Fit B-Spline field using least-squares
        fmap_demean : bool
            Demean voxel-shift map during unwarp
        use_syn : bool
            **Experimental**: Enable ANTs SyN-based susceptibility distortion
            correction (SDC). If fieldmaps are present and enabled, this is not
            run, by default.
        force_syn : bool
            **Temporary**: Always run SyN-based SDC

    """
    qsiprep_wf = Workflow(name='qsiprep_wf')
    qsiprep_wf.base_dir = work_dir

    if freesurfer:
        fsdir = pe.Node(
            BIDSFreeSurferDir(
                derivatives=output_dir,
                freesurfer_home=os.getenv('FREESURFER_HOME'),
                spaces=output_spaces),
            name='fsdir',
            run_without_submitting=True)

    reportlets_dir = os.path.join(work_dir, 'reportlets')
    for subject_id in subject_list:
        single_subject_wf = init_single_subject_wf(
            subject_id=subject_id,
            name="single_subject_" + subject_id + "_wf",
            reportlets_dir=reportlets_dir,
            output_dir=output_dir,
            bids_dir=bids_dir,
            ignore=ignore,
            debug=debug,
            low_mem=low_mem,
            force_spatial_normalization=force_spatial_normalization,
            output_resolution=output_resolution,
            denoise_before_combining=denoise_before_combining,
            dwi_denoise_window=dwi_denoise_window,
            unringing_method=unringing_method,
            dwi_no_biascorr=dwi_no_biascorr,
            no_b0_harmonization=no_b0_harmonization,
            anat_only=anat_only,
            longitudinal=longitudinal,
            b0_threshold=b0_threshold,
            freesurfer=freesurfer,
            hires=hires,
            combine_all_dwis=combine_all_dwis,
            omp_nthreads=omp_nthreads,
            skull_strip_template=skull_strip_template,
            skull_strip_fixed_seed=skull_strip_fixed_seed,
            output_spaces=output_spaces,
            template=template,
            prefer_dedicated_fmaps=prefer_dedicated_fmaps,
            motion_corr_to=motion_corr_to,
            b0_to_t1w_transform=b0_to_t1w_transform,
            intramodal_template_iters=intramodal_template_iters,
            intramodal_template_transform=intramodal_template_transform,
            hmc_model=hmc_model,
            hmc_transform=hmc_transform,
            shoreline_iters=shoreline_iters,
            eddy_config=eddy_config,
            impute_slice_threshold=impute_slice_threshold,
            write_local_bvecs=write_local_bvecs,
            fmap_bspline=fmap_bspline,
            fmap_demean=fmap_demean,
            use_syn=use_syn,
            force_syn=force_syn)

        single_subject_wf.config['execution']['crashdump_dir'] = (os.path.join(
            output_dir, "qsiprep", "sub-" + subject_id, 'log', run_uuid))
        for node in single_subject_wf._get_all_nodes():
            node.config = deepcopy(single_subject_wf.config)
        if freesurfer:
            qsiprep_wf.connect(fsdir, 'subjects_dir', single_subject_wf,
                               'inputnode.subjects_dir')
        else:
            qsiprep_wf.add_nodes([single_subject_wf])

    return qsiprep_wf


def init_single_subject_wf(
        subject_id, name, reportlets_dir, output_dir, bids_dir, ignore, debug, write_local_bvecs,
        low_mem, anat_only, longitudinal, b0_threshold, denoise_before_combining,
        dwi_denoise_window, unringing_method, dwi_no_biascorr, no_b0_harmonization,
        combine_all_dwis, omp_nthreads, skull_strip_template, force_spatial_normalization,
        skull_strip_fixed_seed, freesurfer, hires, output_spaces, template, output_resolution,
        prefer_dedicated_fmaps, motion_corr_to, b0_to_t1w_transform, intramodal_template_iters,
        intramodal_template_transform, hmc_model, hmc_transform, shoreline_iters, eddy_config,
        impute_slice_threshold, fmap_bspline, fmap_demean, use_syn, force_syn):
    """
    This workflow organizes the preprocessing pipeline for a single subject.
    It collects and reports information about the subject, and prepares
    sub-workflows to perform anatomical and diffusion preprocessing.

    Anatomical preprocessing is performed in a single workflow, regardless of
    the number of sessions.
    Diffusion preprocessing is performed using a separate workflow for each
    session's dwi series.

    .. workflow::
        :graph2use: orig
        :simple_form: yes

        from qsiprep.workflows.base import init_single_subject_wf

        wf = init_single_subject_wf(
            subject_id='test',
            name='single_subject_qsipreptest_wf',
            reportlets_dir='.',
            output_dir='.',
            bids_dir='.',
            ignore=[],
            debug=False,
            low_mem=False,
            output_resolution=1.25,
            denoise_before_combining=True,
            dwi_denoise_window=7,
            unringing_method='mrdegibbs',
            dwi_no_biascorr=False,
            no_b0_harmonization=False,
            anat_only=False,
            longitudinal=False,
            b0_threshold=100,
            freesurfer=False,
            hires=False,
            force_spatial_normalization=True,
            combine_all_dwis=True,
            omp_nthreads=1,
            skull_strip_template='OASIS',
            skull_strip_fixed_seed=False,
            output_spaces=['T1w', 'template'],
            template='MNI152NLin2009cAsym',
            prefer_dedicated_fmaps=False,
            motion_corr_to='iterative',
            b0_to_t1w_transform='Rigid',
            intramodal_template_iters=0,
            intramodal_template_transform="Rigid",
            hmc_model='3dSHORE',
            hmc_transform='Affine',
            eddy_config=None,
            shoreline_iters=2,
            impute_slice_threshold=0.0,
            write_local_bvecs=False,
            fmap_bspline=False,
            fmap_demean=True,
            use_syn=False,
            force_syn=False)

    Parameters

        subject_id : str
            List of subject labels
        name : str
            Name of workflow
        ignore : list
            Preprocessing steps to skip (may include "sbref", "fieldmaps")
        debug : bool
            Do inaccurate but fast normalization
        low_mem : bool
            Write uncompressed .nii files in some cases to reduce memory usage
        anat_only : bool
            Disable functional workflows
        longitudinal : bool
            Treat multiple sessions as longitudinal (may increase runtime)
            See sub-workflows for specific differences
        b0_threshold : int
            Images with b-values less than this value will be treated as a b=0 image.
        dwi_denoise_window : int
            window size in voxels for ``dwidenoise``. Must be odd. If 0, '
            '``dwidwenoise`` will not be run'
        unringing_method : str
            algorithm to use for removing Gibbs ringing. Options: none, mrdegibbs
        dwi_no_biascorr : bool
            run spatial bias correction (N4) on dwi series
        no_b0_harmonization : bool
            skip rescaling dwi scans to have matching b=0 intensities across scans
        denoise_before_combining : bool
            'run ``dwidenoise`` before combining dwis. Requires ``combine_all_dwis``'
        combine_all_dwis : Bool
            Combine all dwi sequences within a session into a single data set
        omp_nthreads : int
            Maximum number of threads an individual process may use
        skull_strip_template : str
            Name of ANTs skull-stripping template ('OASIS' or 'NKI')
        skull_strip_fixed_seed : bool
            Do not use a random seed for skull-stripping - will ensure
            run-to-run replicability when used with --omp-nthreads 1
        freesurfer : bool
            Enable FreeSurfer surface reconstruction (may increase runtime)
        hires : bool
            Enable sub-millimeter preprocessing in FreeSurfer
        reportlets_dir : str
            Directory in which to save reportlets
        output_dir : str
            Directory in which to save derivatives
        bids_dir : str
            Root directory of BIDS dataset
        output_spaces : list
            List of output spaces functional images are to be resampled to.
            Some parts of pipeline will only be instantiated for some output
            spaces.

            Valid spaces:

             - T1w
             - template

        template : str
            Name of template targeted by ``template`` output space
        hmc_model : 'none', '3dSHORE' or 'MAPMRI'
            Model used to generate target images for head motion correction. If 'none'
            the transform from the nearest b0 will be used.
        hmc_transform : "Rigid" or "Affine"
            Type of transform used for head motion correction
        impute_slice_threshold : float
            Impute data in slices that are this many SDs from expected. If 0, no slices
            will be imputed.
        motion_corr_to : str
            Motion correct using the 'first' b0 image or use an 'iterative'
            method to motion correct to the midpoint of the b0 images
        eddy_config: str
            Path to a JSON file containing config options for eddy
        fmap_bspline : bool
            **Experimental**: Fit B-Spline field using least-squares
        fmap_demean : bool
            Demean voxel-shift map during unwarp
        use_syn : bool
            **Experimental**: Enable ANTs SyN-based susceptibility distortion
            correction (SDC). If fieldmaps are present and enabled, this is not
            run, by default.
        force_syn : bool
            **Temporary**: Always run SyN-based SDC
        eddy_config: str
            Path to a JSON file containing config options for eddy
        b0_to_t1w_transform : "Rigid" or "Affine"
            Use a rigid or full affine transform for b0-T1w registration
        intramodal_template_iters: int
            Number of iterations for finding the midpoint image from the b0 templates
            from all groups. Has no effect if there is only one group. If 0, all b0
            templates are directly registered to the t1w image.
        intramodal_template_transform: str
            Transformation used for building the intramodal template.


    Inputs

        subjects_dir
            FreeSurfer SUBJECTS_DIR

    """
    if name in ('single_subject_wf', 'single_subject_qsipreptest_wf'):
        # for documentation purposes
        subject_data = {
            't1w': ['/completely/made/up/path/sub-01_T1w.nii.gz'],
            'dwi': ['/completely/made/up/path/sub-01_dwi.nii.gz']
        }
        layout = None
        LOGGER.warning("Building a test workflow")
    else:
        subject_data, layout = collect_data(bids_dir, subject_id)

    # Make sure we always go through these two checks
    if not anat_only and subject_data['dwi'] == []:
        raise Exception("No dwi images found for participant {}. "
                        "All workflows require dwi images.".format(subject_id))

    if not subject_data['t1w']:
        raise Exception("No T1w images found for participant {}. "
                        "All workflows require T1w images.".format(subject_id))

    workflow = Workflow(name=name)
    workflow.__desc__ = """
Results included in this manuscript come from preprocessing
performed using *QSIprep* {qsiprep_ver},
which is based on *Nipype* {nipype_ver}
(@nipype1; @nipype2; RRID:SCR_002502).

""".format(
        qsiprep_ver=__version__, nipype_ver=nipype_ver)
    workflow.__postdesc__ = """

Many internal operations of *qsiprep* use
*Nilearn* {nilearn_ver} [@nilearn, RRID:SCR_001362] and
*Dipy* [@dipy].
For more details of the pipeline, see [the section corresponding
to workflows in *qsiprep*'s documentation]\
(https://qsiprep.readthedocs.io/en/latest/workflows.html \
"qsiprep's documentation").


### References

""".format(nilearn_ver=nilearn_ver)

    inputnode = pe.Node(
        niu.IdentityInterface(fields=['subjects_dir']), name='inputnode')

    bidssrc = pe.Node(
        BIDSDataGrabber(subject_data=subject_data, anat_only=anat_only),
        name='bidssrc')

    bids_info = pe.Node(
        BIDSInfo(), name='bids_info', run_without_submitting=True)

    summary = pe.Node(
        SubjectSummary(output_spaces=output_spaces, template=template),
        name='summary',
        run_without_submitting=True)

    about = pe.Node(
        AboutSummary(version=__version__, command=' '.join(sys.argv)),
        name='about',
        run_without_submitting=True)

    ds_report_summary = pe.Node(
        DerivativesDataSink(base_directory=reportlets_dir, suffix='summary'),
        name='ds_report_summary',
        run_without_submitting=True)

    ds_report_about = pe.Node(
        DerivativesDataSink(base_directory=reportlets_dir, suffix='about'),
        name='ds_report_about',
        run_without_submitting=True)

    # Preprocessing of T1w (includes registration to MNI)
    anat_preproc_wf = init_anat_preproc_wf(
        name="anat_preproc_wf",
        skull_strip_template=skull_strip_template,
        skull_strip_fixed_seed=skull_strip_fixed_seed,
        output_spaces=output_spaces,
        template=template,
        output_resolution=output_resolution,
        force_spatial_normalization=force_spatial_normalization,
        debug=debug,
        longitudinal=longitudinal,
        omp_nthreads=omp_nthreads,
        freesurfer=freesurfer,
        hires=hires,
        reportlets_dir=reportlets_dir,
        output_dir=output_dir,
        num_t1w=len(subject_data['t1w']))

    workflow.connect([
        (inputnode, anat_preproc_wf, [('subjects_dir',
                                       'inputnode.subjects_dir')]),
        (bidssrc, bids_info, [(('t1w', fix_multi_T1w_source_name),
                               'in_file')]),
        (inputnode, summary, [('subjects_dir', 'subjects_dir')]),
        (bidssrc, summary, [('t1w', 't1w'), ('t2w', 't2w')]),
        (bids_info, summary, [('subject_id', 'subject_id')]),
        (bidssrc, anat_preproc_wf, [('t1w', 'inputnode.t1w'),
                                    ('t2w', 'inputnode.t2w'),
                                    ('roi', 'inputnode.roi'),
                                    ('flair', 'inputnode.flair')]),
        (summary, anat_preproc_wf, [('subject_id', 'inputnode.subject_id')]),
        (bidssrc, ds_report_summary, [(('t1w', fix_multi_T1w_source_name),
                                       'source_file')]),
        (summary, ds_report_summary, [('out_report', 'in_file')]),
        (bidssrc, ds_report_about, [(('t1w', fix_multi_T1w_source_name),
                                     'source_file')]),
        (about, ds_report_about, [('out_report', 'in_file')]),
    ])

    if anat_only:
        return workflow

    if impute_slice_threshold > 0 and hmc_model == "none":
        LOGGER.warning("hmc_model must not be 'none' if slices are to be imputed. "
                       "setting `impute_slice_threshold=0`")
        impute_slice_threshold = 0

    # Handle the grouping of multiple dwi files within a session
    dwi_fmap_groups = group_dwi_scans(layout, subject_data,
                                      using_fsl=hmc_model == 'eddy',
                                      combine_scans=combine_all_dwis,
                                      ignore_fieldmaps="fieldmaps" in ignore)
    LOGGER.info(dwi_fmap_groups)

    outputs_to_files = {dwi_group['concatenated_bids_name']: dwi_group
                        for dwi_group in dwi_fmap_groups}
    if force_syn:
        for group_name in outputs_to_files:
            outputs_to_files[group_name]['fieldmap_info'] = {"suffix": "syn"}
    summary.inputs.dwi_groupings = outputs_to_files

    make_intramodal_template = False
    if intramodal_template_iters > 0:
        if len(outputs_to_files) < 2:
            raise Exception("Cannot make an intramodal with less than 2 groups.")
        make_intramodal_template = True

    intramodal_template_wf = init_intramodal_template_wf(
        omp_nthreads=omp_nthreads,
        t1w_source_file=fix_multi_T1w_source_name(subject_data['t1w']),
        reportlets_dir=reportlets_dir,
        num_iterations=intramodal_template_iters,
        transform=intramodal_template_transform,
        inputs_list=sorted(outputs_to_files.keys()),
        name="intramodal_template_wf")

    if make_intramodal_template:
        workflow.connect([
            (anat_preproc_wf, intramodal_template_wf, [
                ('outputnode.t1_preproc', 'inputnode.t1_preproc'),
                ('outputnode.t1_brain', 'inputnode.t1_brain'),
                ('outputnode.t1_mask', 'inputnode.t1_mask'),
                ('outputnode.t1_seg', 'inputnode.t1_seg'),
                ('outputnode.t1_aseg', 'inputnode.t1_aseg'),
                ('outputnode.t1_aparc', 'inputnode.t1_aparc'),
                ('outputnode.t1_tpms', 'inputnode.t1_tpms'),
                ('outputnode.t1_2_mni_forward_transform',
                 'inputnode.t1_2_mni_forward_transform'),
                ('outputnode.t1_2_mni_reverse_transform',
                 'inputnode.t1_2_mni_reverse_transform'),
                ('outputnode.dwi_sampling_grid',
                 'inputnode.dwi_sampling_grid'),
                # Undefined if --no-freesurfer, but this is safe
                ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
                ('outputnode.subject_id', 'inputnode.subject_id'),
                ('outputnode.t1_2_fsnative_forward_transform',
                 'inputnode.t1_2_fsnative_forward_transform'),
                ('outputnode.t1_2_fsnative_reverse_transform',
                 'inputnode.t1_2_fsnative_reverse_transform')])])

    # create a processing pipeline for the dwis in each session
    for output_fname, dwi_info in outputs_to_files.items():
        source_file = get_source_file(dwi_info['dwi_series'], output_fname, suffix="_dwi")
        dwi_preproc_wf = init_dwi_preproc_wf(
            scan_groups=dwi_info,
            output_prefix=output_fname,
            layout=layout,
            ignore=ignore,
            b0_threshold=b0_threshold,
            dwi_denoise_window=dwi_denoise_window,
            unringing_method=unringing_method,
            dwi_no_biascorr=dwi_no_biascorr,
            no_b0_harmonization=no_b0_harmonization,
            denoise_before_combining=denoise_before_combining,
            motion_corr_to=motion_corr_to,
            b0_to_t1w_transform=b0_to_t1w_transform,
            hmc_model=hmc_model,
            hmc_transform=hmc_transform,
            shoreline_iters=shoreline_iters,
            eddy_config=eddy_config,
            impute_slice_threshold=impute_slice_threshold,
            reportlets_dir=reportlets_dir,
            output_spaces=output_spaces,
            template=template,
            output_dir=output_dir,
            omp_nthreads=omp_nthreads,
            low_mem=low_mem,
            fmap_bspline=fmap_bspline,
            fmap_demean=fmap_demean,
            use_syn=use_syn,
            force_syn=force_syn,
            sloppy=debug,
            source_file=source_file
        )
        dwi_finalize_wf = init_dwi_finalize_wf(
            scan_groups=dwi_info,
            name=dwi_preproc_wf.name.replace('dwi_preproc', 'dwi_finalize'),
            output_prefix=output_fname,
            layout=layout,
            ignore=ignore,
            hmc_model=hmc_model,
            shoreline_iters=shoreline_iters,
            write_local_bvecs=write_local_bvecs,
            reportlets_dir=reportlets_dir,
            output_spaces=output_spaces,
            template=template,
            output_resolution=output_resolution,
            output_dir=output_dir,
            omp_nthreads=omp_nthreads,
            use_syn=use_syn,
            low_mem=low_mem,
            make_intramodal_template=make_intramodal_template,
            source_file=source_file
        )

        workflow.connect([
            (
                anat_preproc_wf,
                dwi_preproc_wf,
                [
                    ('outputnode.t1_preproc', 'inputnode.t1_preproc'),
                    ('outputnode.t1_brain', 'inputnode.t1_brain'),
                    ('outputnode.t1_mask', 'inputnode.t1_mask'),
                    ('outputnode.t1_seg', 'inputnode.t1_seg'),
                    ('outputnode.t1_aseg', 'inputnode.t1_aseg'),
                    ('outputnode.t1_aparc', 'inputnode.t1_aparc'),
                    ('outputnode.t1_tpms', 'inputnode.t1_tpms'),
                    ('outputnode.t1_2_mni_forward_transform',
                     'inputnode.t1_2_mni_forward_transform'),
                    ('outputnode.t1_2_mni_reverse_transform',
                     'inputnode.t1_2_mni_reverse_transform'),
                    ('outputnode.dwi_sampling_grid',
                     'inputnode.dwi_sampling_grid'),
                    # Undefined if --no-freesurfer, but this is safe
                    ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
                    ('outputnode.subject_id', 'inputnode.subject_id'),
                    ('outputnode.t1_2_fsnative_forward_transform',
                     'inputnode.t1_2_fsnative_forward_transform'),
                    ('outputnode.t1_2_fsnative_reverse_transform',
                     'inputnode.t1_2_fsnative_reverse_transform')
                ]),
            (
                anat_preproc_wf,
                dwi_finalize_wf,
                [
                    ('outputnode.t1_preproc', 'inputnode.t1_preproc'),
                    ('outputnode.t1_brain', 'inputnode.t1_brain'),
                    ('outputnode.t1_mask', 'inputnode.t1_mask'),
                    ('outputnode.t1_seg', 'inputnode.t1_seg'),
                    ('outputnode.t1_aseg', 'inputnode.t1_aseg'),
                    ('outputnode.t1_aparc', 'inputnode.t1_aparc'),
                    ('outputnode.t1_tpms', 'inputnode.t1_tpms'),
                    ('outputnode.t1_2_mni_forward_transform',
                     'inputnode.t1_2_mni_forward_transform'),
                    ('outputnode.t1_2_mni_reverse_transform',
                     'inputnode.t1_2_mni_reverse_transform'),
                    ('outputnode.dwi_sampling_grid',
                     'inputnode.dwi_sampling_grid'),
                    ('outputnode.mni_mask', 'inputnode.mni_mask'),
                    # Undefined if --no-freesurfer, but this is safe
                    ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
                    ('outputnode.subject_id', 'inputnode.subject_id'),
                    ('outputnode.t1_2_fsnative_forward_transform',
                     'inputnode.t1_2_fsnative_forward_transform'),
                    ('outputnode.t1_2_fsnative_reverse_transform',
                     'inputnode.t1_2_fsnative_reverse_transform')
                ]),
            (
                dwi_preproc_wf,
                dwi_finalize_wf,
                [
                    ('outputnode.dwi_files', 'inputnode.dwi_files'),
                    ('outputnode.cnr_map', 'inputnode.cnr_map'),
                    ('outputnode.bval_files', 'inputnode.bval_files'),
                    ('outputnode.bvec_files', 'inputnode.bvec_files'),
                    ('outputnode.b0_ref_image', 'inputnode.b0_ref_image'),
                    ('outputnode.b0_indices', 'inputnode.b0_indices'),
                    ('outputnode.hmc_xforms', 'inputnode.hmc_xforms'),
                    ('outputnode.fieldwarps', 'inputnode.fieldwarps'),
                    ('outputnode.itk_b0_to_t1', 'inputnode.itk_b0_to_t1'),
                    ('outputnode.hmc_optimization_data', 'inputnode.hmc_optimization_data'),
                    ('outputnode.raw_qc_file', 'inputnode.raw_qc_file'),
                    ('outputnode.coreg_score', 'inputnode.coreg_score'),
                    ('outputnode.raw_concatenated', 'inputnode.raw_concatenated'),
                    ('outputnode.confounds', 'inputnode.confounds'),
                    ('outputnode.carpetplot_data', 'inputnode.carpetplot_data')
                    ])
        ])

        if make_intramodal_template:
            input_name = 'inputnode.{name}_b0_template'.format(
                name=output_fname.replace('-', '_'))
            output_name = 'outputnode.{name}_transform'.format(
                name=output_fname.replace('-', '_'))
            workflow.connect([
                (dwi_preproc_wf, intramodal_template_wf, [
                    ('outputnode.b0_ref_image', input_name)]),
                (intramodal_template_wf, dwi_finalize_wf, [
                    (output_name, 'inputnode.b0_to_intramodal_template_transforms'),
                    ('outputnode.intramodal_template_to_t1_affine',
                     'inputnode.intramodal_template_to_t1_affine'),
                    ('outputnode.intramodal_template_to_t1_warp',
                     'inputnode.intramodal_template_to_t1_warp'),
                    ('outputnode.intramodal_template',
                     'inputnode.intramodal_template')])
            ])

    return workflow
