from __future__ import annotations

# internal imports
import argparse

# other imports
import os
import time

import numpy as np
import pandas as pd
from wsi_core.batch_process_utils import initialize_df
from wsi_core.WholeSlideImage import WholeSlideImage
from wsi_core.wsi_utils import StitchCoords


def stitching(file_path, wsi_object, downscale=64):
    start = time.time()
    heatmap = StitchCoords(
        file_path,
        wsi_object,
        downscale=downscale,
        bg_color=(0, 0, 0),
        alpha=-1,
        draw_grid=False,
    )
    total_time = time.time() - start

    return heatmap, total_time


def segment(WSI_object, seg_params=None, filter_params=None, mask_file=None):
    ### Start Seg Timer
    start_time = time.time()
    # Use segmentation file
    if mask_file is not None:
        WSI_object.initSegmentation(mask_file)
    # Segment
    else:
        WSI_object.segmentTissue(**seg_params, filter_params=filter_params)

    ### Stop Seg Timers
    seg_time_elapsed = time.time() - start_time
    return WSI_object, seg_time_elapsed


def patching(WSI_object, **kwargs):
    ### Start Patch Timer
    start_time = time.time()

    # Patch
    file_path = WSI_object.process_contours(**kwargs)

    ### Stop Patch Timer
    patch_time_elapsed = time.time() - start_time
    return file_path, patch_time_elapsed


def _get_mpp_openslide(slide_path: str) -> float | None:
    import openslide

    with openslide.OpenSlide(slide_path) as slide:
        try:
            mppx = float(slide.properties[openslide.PROPERTY_NAME_MPP_X])
            mppy = float(slide.properties[openslide.PROPERTY_NAME_MPP_Y])
            return (mppx + mppy) / 2
        except Exception:
            return None


def _get_mpp_tiffslide(slide_path: str) -> float | None:
    """Read MPP using TiffSlide."""
    import tiffslide

    with tiffslide.TiffSlide(slide_path) as slide:
        mppx: float | None = None
        mppy: float | None = None
        if (
            tiffslide.PROPERTY_NAME_MPP_X in slide.properties
            and tiffslide.PROPERTY_NAME_MPP_Y in slide.properties
        ):
            mppx = slide.properties[tiffslide.PROPERTY_NAME_MPP_X]
            mppy = slide.properties[tiffslide.PROPERTY_NAME_MPP_Y]
            if mppx is None or mppy is None:
                return None
            else:
                try:
                    mppx = float(mppx)
                    mppy = float(mppy)
                    return (mppx + mppy) / 2
                except Exception:
                    return None
    return None


def seg_and_patch(
    source,
    save_dir,
    patch_save_dir,
    mask_save_dir,
    stitch_save_dir,
    patch_size=256,
    step_size=256,
    seg_params={
        "seg_level": -1,
        "sthresh": 8,
        "mthresh": 7,
        "close": 4,
        "use_otsu": False,
        "keep_ids": "none",
        "exclude_ids": "none",
    },
    filter_params={"a_t": 100, "a_h": 16, "max_n_holes": 8},
    vis_params={"vis_level": -1, "line_thickness": 500},
    patch_params={"use_padding": True, "contour_fn": "four_pt"},
    patch_level=0,
    use_default_params=False,
    seg=False,
    save_mask=True,
    stitch=False,
    patch=False,
    auto_skip=True,
    process_list=None,
    patch_spacing=None,
):
    slides = sorted(os.listdir(source))
    slides = [slide for slide in slides if os.path.isfile(os.path.join(source, slide))]
    if process_list is None:
        df = initialize_df(slides, seg_params, filter_params, vis_params, patch_params)

    else:
        df = pd.read_csv(process_list)
        df = initialize_df(df, seg_params, filter_params, vis_params, patch_params)

    mask = df["process"] == 1
    process_stack = df[mask]

    total = len(process_stack)

    legacy_support = "a" in df.keys()
    if legacy_support:
        print("detected legacy segmentation csv file, legacy support enabled")
        df = df.assign(
            **{
                "a_t": np.full((len(df)), int(filter_params["a_t"]), dtype=np.uint32),
                "a_h": np.full((len(df)), int(filter_params["a_h"]), dtype=np.uint32),
                "max_n_holes": np.full(
                    (len(df)), int(filter_params["max_n_holes"]), dtype=np.uint32
                ),
                "line_thickness": np.full(
                    (len(df)), int(vis_params["line_thickness"]), dtype=np.uint32
                ),
                "contour_fn": np.full((len(df)), patch_params["contour_fn"]),
            }
        )

    seg_times = 0.0
    patch_times = 0.0
    stitch_times = 0.0

    orig_patch_size = patch_size

    for i in range(total):
        df.to_csv(os.path.join(save_dir, "process_list_autogen.csv"), index=False)
        idx = process_stack.index[i]
        slide = process_stack.loc[idx, "slide_id"]
        print("\n\nprogress: {:.2f}, {}/{}".format(i / total, i, total))
        print("processing {}".format(slide))

        df.loc[idx, "process"] = 0
        slide_id, _ = os.path.splitext(slide)

        if auto_skip and os.path.isfile(os.path.join(patch_save_dir, slide_id + ".h5")):
            print("{} already exist in destination location, skipped".format(slide_id))
            df.loc[idx, "status"] = "already_exist"
            continue

        # Inialize WSI
        full_path = os.path.join(source, slide)
        try:
            WSI_object = WholeSlideImage(full_path)
        except Exception as e:
            print("!" * 80)
            print("ERROR:")
            print(e)
            print("!" * 80)
            continue

        if use_default_params:
            current_vis_params = vis_params.copy()
            current_filter_params = filter_params.copy()
            current_seg_params = seg_params.copy()
            current_patch_params = patch_params.copy()

        else:
            current_vis_params = {}
            current_filter_params = {}
            current_seg_params = {}
            current_patch_params = {}

            for key in vis_params.keys():
                if legacy_support and key == "vis_level":
                    df.loc[idx, key] = -1
                current_vis_params.update({key: df.loc[idx, key]})

            for key in filter_params.keys():
                if legacy_support and key == "a_t":
                    old_area = df.loc[idx, "a"]
                    seg_level = df.loc[idx, "seg_level"]
                    scale = WSI_object.level_downsamples[seg_level]
                    adjusted_area = int(old_area * (scale[0] * scale[1]) / (512 * 512))
                    current_filter_params.update({key: adjusted_area})
                    df.loc[idx, key] = adjusted_area
                current_filter_params.update({key: df.loc[idx, key]})

            for key in seg_params.keys():
                if legacy_support and key == "seg_level":
                    df.loc[idx, key] = -1
                current_seg_params.update({key: df.loc[idx, key]})

            for key in patch_params.keys():
                current_patch_params.update({key: df.loc[idx, key]})

        if current_vis_params["vis_level"] < 0:
            if len(WSI_object.level_dim) == 1:
                current_vis_params["vis_level"] = 0

            else:
                wsi = WSI_object.getOpenSlide()
                best_level = wsi.get_best_level_for_downsample(64)
                current_vis_params["vis_level"] = best_level

        if current_seg_params["seg_level"] < 0:
            if len(WSI_object.level_dim) == 1:
                current_seg_params["seg_level"] = 0

            else:
                wsi = WSI_object.getOpenSlide()
                best_level = wsi.get_best_level_for_downsample(64)
                current_seg_params["seg_level"] = best_level

        keep_ids = str(current_seg_params["keep_ids"])
        if keep_ids != "none" and len(keep_ids) > 0:
            str_ids = current_seg_params["keep_ids"]
            current_seg_params["keep_ids"] = np.array(str_ids.split(",")).astype(int)
        else:
            current_seg_params["keep_ids"] = []

        exclude_ids = str(current_seg_params["exclude_ids"])
        if exclude_ids != "none" and len(exclude_ids) > 0:
            str_ids = current_seg_params["exclude_ids"]
            current_seg_params["exclude_ids"] = np.array(str_ids.split(",")).astype(int)
        else:
            current_seg_params["exclude_ids"] = []

        w, h = WSI_object.level_dim[current_seg_params["seg_level"]]
        if w * h > 1e8:
            print(
                "level_dim {} x {} is likely too large for successful segmentation, aborting".format(
                    w, h
                )
            )
            df.loc[idx, "status"] = "failed_seg"
            continue

        df.loc[idx, "vis_level"] = current_vis_params["vis_level"]
        df.loc[idx, "seg_level"] = current_seg_params["seg_level"]

        seg_time_elapsed = -1
        if seg:
            try:
                WSI_object, seg_time_elapsed = segment(
                    WSI_object, current_seg_params, current_filter_params
                )
            except Exception:
                print("!!!! failed to patch")
                continue

        if save_mask:
            mask = WSI_object.visWSI(**current_vis_params)
            mask_path = os.path.join(mask_save_dir, slide_id + ".jpg")
            mask.save(mask_path)

        patch_time_elapsed = -1  # Default time
        if patch:
            # -----------------------------------------------------------------------
            # Added by Jakub Kaczmarzyk (github kaczmarj) to get patch size for a
            # particular spacing. The patching happens at the highest resolution, but
            # we want to extract patches at a particular spacing.
            if patch_spacing is not None:
                try:
                    slide_mpp = _get_mpp_tiffslide(full_path)
                except Exception as e:
                    print("!" * 80)
                    print("EXCEPTION while getting MPP")
                    print(e)
                    print("!" * 80)
                    continue

                try:
                    if slide_mpp is None:
                        slide_mpp = _get_mpp_openslide(full_path)
                except Exception as e:
                    print("!" * 80)
                    print("EXCEPTION while getting MPP using OpenSlide")
                    print(e)
                    print("!" * 80)

                if slide_mpp is None:
                    print("!" * 40)
                    print("SKIPPING this slide because I cannot find the spacing!")
                    print(full_path)
                    print("!" * 40)
                    continue

                patch_size = orig_patch_size * patch_spacing / slide_mpp
                patch_size = round(patch_size)

                # Use non-overlapping patches by default.
                print("USING NON-OVERLAPPING PATCHES")
                step_size = patch_size
                print(f"Slide MPP = {slide_mpp}")
                print(
                    f"Patch size = {patch_size} pixels ({patch_size * slide_mpp:0.1f} um)"
                )
                print(
                    f"Step size = {step_size} pixels ({step_size * slide_mpp:0.1f} um)"
                )
            # ----------------------------------------------------------------------

            current_patch_params.update(
                {
                    "patch_level": patch_level,
                    "patch_size": patch_size,
                    "step_size": step_size,
                    "save_path": patch_save_dir,
                }
            )
            file_path, patch_time_elapsed = patching(
                WSI_object=WSI_object,
                **current_patch_params,
            )

        stitch_time_elapsed = -1
        if stitch:
            file_path = os.path.join(patch_save_dir, slide_id + ".h5")
            if os.path.isfile(file_path):
                heatmap, stitch_time_elapsed = stitching(
                    file_path, WSI_object, downscale=64
                )
                stitch_path = os.path.join(stitch_save_dir, slide_id + ".jpg")
                heatmap.save(stitch_path)

        print("segmentation took {} seconds".format(seg_time_elapsed))
        print("patching took {} seconds".format(patch_time_elapsed))
        print("stitching took {} seconds".format(stitch_time_elapsed))
        df.loc[idx, "status"] = "processed"

        seg_times += seg_time_elapsed
        patch_times += patch_time_elapsed
        stitch_times += stitch_time_elapsed

    seg_times /= total
    patch_times /= total
    stitch_times /= total

    df.to_csv(os.path.join(save_dir, "process_list_autogen.csv"), index=False)
    print("average segmentation time in s per slide: {}".format(seg_times))
    print("average patching time in s per slide: {}".format(patch_times))
    print("average stiching time in s per slide: {}".format(stitch_times))

    return seg_times, patch_times


parser = argparse.ArgumentParser(description="seg and patch")
parser.add_argument(
    "--source", type=str, help="path to folder containing raw wsi image files"
)
parser.add_argument("--step_size", type=int, default=256, help="step_size")
parser.add_argument("--patch_size", type=int, default=256, help="patch_size")
parser.add_argument("--patch", default=False, action="store_true")
parser.add_argument("--seg", default=False, action="store_true")
parser.add_argument("--stitch", default=False, action="store_true")
parser.add_argument("--no_auto_skip", default=True, action="store_false")
parser.add_argument("--save_dir", type=str, help="directory to save processed data")
parser.add_argument(
    "--preset",
    default=None,
    type=str,
    help="predefined profile of default segmentation and filter parameters (.csv)",
)
parser.add_argument(
    "--patch_level", type=int, default=0, help="downsample level at which to patch"
)
parser.add_argument(
    "--process_list",
    type=str,
    default=None,
    help="name of list of images to process with parameters (.csv)",
)
parser.add_argument("--patch_spacing", type=float, default=0.25, help="MPP of patches")

if __name__ == "__main__":
    args = parser.parse_args()

    patch_save_dir = os.path.join(args.save_dir, "patches")
    mask_save_dir = os.path.join(args.save_dir, "masks")
    stitch_save_dir = os.path.join(args.save_dir, "stitches")

    if args.process_list:
        process_list = os.path.join(args.save_dir, args.process_list)

    else:
        process_list = None

    print("source: ", args.source)
    print("patch_save_dir: ", patch_save_dir)
    print("mask_save_dir: ", mask_save_dir)
    print("stitch_save_dir: ", stitch_save_dir)

    directories = {
        "source": args.source,
        "save_dir": args.save_dir,
        "patch_save_dir": patch_save_dir,
        "mask_save_dir": mask_save_dir,
        "stitch_save_dir": stitch_save_dir,
    }

    for key, val in directories.items():
        print("{} : {}".format(key, val))
        if key not in ["source"]:
            os.makedirs(val, exist_ok=True)

    seg_params = {
        "seg_level": -1,
        "sthresh": 8,
        "mthresh": 7,
        "close": 4,
        "use_otsu": False,
        "keep_ids": "none",
        "exclude_ids": "none",
    }
    filter_params = {"a_t": 100, "a_h": 16, "max_n_holes": 8}
    vis_params = {"vis_level": -1, "line_thickness": 250}
    patch_params = {"use_padding": True, "contour_fn": "four_pt"}

    if args.preset:
        preset_df = pd.read_csv(os.path.join("presets", args.preset))
        for key in seg_params.keys():
            seg_params[key] = preset_df.loc[0, key]

        for key in filter_params.keys():
            filter_params[key] = preset_df.loc[0, key]

        for key in vis_params.keys():
            vis_params[key] = preset_df.loc[0, key]

        for key in patch_params.keys():
            patch_params[key] = preset_df.loc[0, key]

    parameters = {
        "seg_params": seg_params,
        "filter_params": filter_params,
        "patch_params": patch_params,
        "vis_params": vis_params,
    }

    print(parameters)

    seg_times, patch_times = seg_and_patch(
        **directories,
        **parameters,
        patch_size=args.patch_size,
        step_size=args.step_size,
        seg=args.seg,
        use_default_params=False,
        save_mask=True,
        stitch=args.stitch,
        patch_level=args.patch_level,
        patch=args.patch,
        process_list=process_list,
        auto_skip=args.no_auto_skip,
        patch_spacing=args.patch_spacing,
    )
