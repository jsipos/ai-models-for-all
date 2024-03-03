"""A Modal application for running `ai-models` weather forecasts."""

import datetime
import os
import pathlib
import shutil

import modal
from ai_models import model
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from . import ai_models_shim, config, gcs
from .app import stub, volume

config.set_logger_basic_config()
logger = config.get_logger(__name__, add_handler=False)


@stub.function(
    image=stub.image,
    secrets=[config.ENV_SECRETS],
    network_file_systems={str(config.CACHE_DIR): volume},
    timeout=300,
)
def prepare_gfs_analysis(
    model_name: str = "panguweather",
    model_init: datetime.datetime = datetime.datetime(2023, 7, 1, 0, 0),
    force: bool = config.FORCE_OVERRIDE,
):
    """Retrieve and prepare initial conditions from the GFS/GDAS to run with an AI model.

    Parameters
    ----------
    model_name : str
        Short name for the model to run; must be one of ['panguweather', 'fourcastnet_v2',
        'graphcast']. Defaults to 'panguweather'.
    model_init : datetime.datetime
        Target initialization time or model epoch to fetch.
    force : bool
        Force re-download and processing, even if the target file already exists.

    """
    from . import gfs

    logger.info(f"Preparing GFS/GDAS initial conditions for {model_name} model run...")

    template_pth = config.make_gfs_template_path(model_name)
    if not template_pth.exists():
        raise ValueError(
            f"Expected to find GFS/GDAS -> ERA-5 template at {template_pth}, but file does not exist."
        )

    gdas_base_pth = gfs.make_gfs_base_pth(model_init)
    gdas_base_pth.mkdir(parents=True, exist_ok=True)

    proc_gdas_fn = f"gdas.proc-{model_name}.grib"
    final_proc_gdas_pth = gdas_base_pth / proc_gdas_fn

    # Short-circuit - don't waste our time if file already exists.
    if final_proc_gdas_pth.exists() and not force:
        logger.info(
            f"Found existing processed GFS/GDAS file {gdas_base_pth / proc_gdas_fn};"
            " skipping download and processing."
        )
        return

    service_account_info = gcs.get_service_account_json("GCS_SERVICE_ACCOUNT_INFO")
    gcs_handler = gcs.GoogleCloudStorageHandler.with_service_account_info(
        service_account_info
    )

    # Set up the files to download with useful metadata (e.g. time lags)
    match model_name:
        case "panguweather" | "fourcastnetv2-small":
            model_init_tds = [
                datetime.timedelta(hours=0),
            ]
            source_blob_names = [
                gfs.make_gfs_ics_blob_name(model_init + td) for td in model_init_tds
            ]
        case "graphcast":
            # By convention, the first element is the init time, and the second element
            # is the time-lagged input.
            model_init_tds = [
                datetime.timedelta(hours=0),
                datetime.timedelta(hours=-6),
            ]
            source_blob_names = [
                gfs.make_gfs_ics_blob_name(model_init + td) for td in model_init_tds
            ]
        case _:
            raise ValueError(f"Encountered unknown model {model_name}")

    source_fns = [blob_name.split("/")[-1] for blob_name in source_blob_names]
    for source_blob_name, source_fn in zip(source_blob_names, source_fns):
        logger.info(
            f"Attempting to download GFS/GDAS blob gs://{gfs.GFS_BUCKET}/{source_blob_name}..."
        )
        gcs_handler.download_blob(gfs.GFS_BUCKET, source_blob_name, source_fn)

        # Sanity check to make sure we were able to download the GDAS file.
        if not pathlib.Path(source_fn).exists():
            raise RuntimeError("Failed to download GFS/GDAS blob.")

    # Run subsetting
    logger.info("Subsetting GFS/GDAS data...")
    match model_name:
        case "panguweather" | "fourcastnetv2-small":
            # There should only be one file that we downloaded, so we can just directly
            # use it.
            source_fn = source_fns[0]
            logger.info("Processing Set 1 -> %s", source_fn)
            subset_grbs = gfs.process_gdas_grib(template_pth, source_fn, model_init)
        case "graphcast":
            # Use our slightly custom logic.
            # TODO: Re-factor this to its own stand-alone function for cleanliness.

            # Timedeltas for Set 1 - the 0- and 6-hr lagged messages
            # NOTE: these should match the deltas in model_init_tds above; ideally we should
            # just re-use those directly.
            template_tds = [datetime.timedelta(hours=0), datetime.timedelta(hours=-6)]
            # Timedeltas for Set 2 (precipitation) - due to some quirkiness in the ai-models package,
            # we use 6- and 18-hr offsets for the 0- and 6-hr lagged messages, respectively.
            tp_template_tds = [
                datetime.timedelta(hours=-6),
                datetime.timedelta(hours=-18),
            ]
            subset_grbs = []
            # Set 1 - Core fields (everything but precipitation)
            for source_fn, template_td in zip(source_fns, template_tds):
                logger.info("Processing Set 1 (core fields) -> %s", source_fn)
                template_dt = config.DEFAULT_GFS_TEMPLATE_MODEL_EPOCH + template_td
                extra_template_matchers = {
                    "dataDate": int(template_dt.strftime("%Y%m%d")),
                    "dataTime": int(template_dt.strftime("%H%M")),
                    "shortName": lambda x: x != "tp",
                }
                output_msgs = gfs.process_gdas_grib(
                    template_pth,
                    pathlib.Path(source_fn),
                    # Offset the model_init time by the expected timedelta so that we
                    # appropriately encode the GRIB message timestamps.
                    model_init + template_td,
                    extra_template_matchers=extra_template_matchers,
                )
                subset_grbs.extend(output_msgs)
            # Set 2) - Precipitation; use the alternate time deltas and hardcode the precipitation
            # field.
            for source_fn, template_td in zip(source_fns, tp_template_tds):
                logger.info("Processing Set 2 (precipitation) -> %s", source_fn)
                template_dt = config.DEFAULT_GFS_TEMPLATE_MODEL_EPOCH + template_td
                extra_template_matchers = {
                    "dataDate": int(template_dt.strftime("%Y%m%d")),
                    "dataTime": int(template_dt.strftime("%H%M")),
                    "shortName": "tp",
                }
                output_msgs = gfs.process_gdas_grib(
                    template_pth,
                    pathlib.Path(source_fn),
                    model_init + template_td,
                    extra_template_matchers=extra_template_matchers,
                )
                subset_grbs.extend(output_msgs)
        case _:
            raise ValueError(f"Encountered unknown model {model_name}")

    with (
        open(proc_gdas_fn, "wb") as f,
        logging_redirect_tqdm(
            loggers=[
                logger,
            ]
        ),
    ):
        for grb in tqdm(
            subset_grbs,
            unit="msg",
            total=len(subset_grbs),
            desc="GRIB messages",
        ):
            msg = grb.tostring()
            f.write(msg)
    logger.info(
        "Copying processed GFS/GDAS file to cache at %s...",
        final_proc_gdas_pth,
    )
    shutil.copy(proc_gdas_fn, final_proc_gdas_pth)
    logger.info("... done.")

    # Sanity check to make sure that we wrote out the processed GDAS file.
    if not (gdas_base_pth / proc_gdas_fn).exists():
        raise RuntimeError("Failed to produce subset GFS/GDAS GRIB.")


@stub.function(
    image=stub.image,
    secrets=[config.ENV_SECRETS],
    network_file_systems={str(config.CACHE_DIR): volume},
    # gpu="T4",
    timeout=60,
    allow_cross_region_volumes=True,
)
def check_assets(skip_validate_env: bool = False):
    """This is a placeholder function for testing that the application and credentials
    are all set up correctly and working as expected."""
    import cdsapi

    if not skip_validate_env:
        config.validate_env()

    logger.info(f"Running locally -> {modal.is_local()}")

    assets = list(config.AI_MODEL_ASSETS_DIR.glob("**/*"))
    logger.info(f"Found {len(assets)} assets:")
    for i, asset in enumerate(assets, 1):
        logger.info(f"({i}) {asset}")
    logger.info(f"CDS API URL: {os.environ['CDSAPI_URL']}")
    logger.info(f"CDS API Key: {os.environ['CDSAPI_KEY']}")

    client = cdsapi.Client()
    logger.info(client)

    test_cdsapirc = pathlib.Path("~/.cdsapirc").expanduser()
    logger.info(f"Test .cdsapirc: {test_cdsapirc} exists = {test_cdsapirc.exists()}")

    logger.info("Trying to import eccodes...")
    # NOTE: Right now, this will throw a UserWarning: "libexpat.so.1: cannot
    # open shared object file: No such file or directory." This is likely due to
    # something not being built correctly by mamba in the application image, but
    # it doesn't impact functionality at the moment.
    import eccodes

    logger.info("Getting GPU information...")
    import onnxruntime as ort

    logger.info(
        f"ort avail providers: {ort.get_available_providers()}"
    )  # output: ['CUDAExecutionProvider', 'CPUExecutionProvider']
    logger.info(f"onnxruntime device: {ort.get_device()}")  # output: GPU

    logger.info(f"Checking contents on network file system at {config.CACHE_DIR}...")
    for i, asset in enumerate(config.CACHE_DIR.glob("**/*"), 1):
        logger.info(f"({i}) {asset}")

    logger.info("Checking for access to GCS...")

    service_account_info = gcs.get_service_account_json("GCS_SERVICE_ACCOUNT_INFO")
    gcs_handler = gcs.GoogleCloudStorageHandler.with_service_account_info(
        service_account_info
    )
    bucket_name = os.environ["GCS_BUCKET_NAME"]
    logger.info(f"Listing blobs in GCS bucket gs://{bucket_name}")
    blobs = list(gcs_handler.client.list_blobs(bucket_name))
    logger.info(f"Found {len(blobs)} blobs:")
    for i, blob in enumerate(blobs, 1):
        logger.info(f"({i}) {blob.name}")


@stub.cls(
    secrets=[config.ENV_SECRETS],
    gpu=config.DEFAULT_GPU_CONFIG,
    network_file_systems={str(config.CACHE_DIR): volume},
    concurrency_limit=1,
    timeout=1_800,
)
class AIModel:
    def __init__(
        self,
        # TODO: Re-factor arguments into a well-structured dataclass.
        model_name: str = "panguweather",
        model_init: datetime.datetime = datetime.datetime(2023, 7, 1, 0, 0),
        lead_time: int = 12,
        use_gfs: bool = False,
    ) -> None:
        self.model_name = model_name
        self.model_init = model_init

        # Cap forecast lead time to 10 days; the models may or may not work longer than
        # this, but this is an unnecessary foot-gun. A savvy user can disable this check
        # in-code.
        if lead_time > config.MAX_FCST_LEAD_TIME:
            logger.warning(
                f"Requested forecast lead time ({lead_time}) exceeds max; setting"
                f" to {config.MAX_FCST_LEAD_TIME}. You can manually set a higher limit in"
                "ai-models-modal/config.py::MAX_FCST_LEAD_TIME."
            )
            self.lead_time = config.MAX_FCST_LEAD_TIME
        else:
            self.lead_time = lead_time

        self.out_pth = config.make_output_path(model_name, model_init, use_gfs)
        self.out_pth.parent.mkdir(parents=True, exist_ok=True)

        self.use_gfs = use_gfs

    @modal.enter()
    def _initialize_model(self):
        logger.info(f"   Model: {self.model_name}")
        logger.info(f"   Run initialization datetime: {self.model_init}")
        logger.info(f"   Forecast lead time: {self.lead_time}")
        logger.info(f"   Model output path: {str(self.out_pth)}")
        logger.info(
            f"   Initial conditions source: {'gfs' if self.use_gfs else 'era5'}"
        )
        logger.info("Running model initialization / staging...")
        if self.use_gfs:
            self.init_model = self._init_model_for_gfs()
        else:
            self.init_model = self._init_model_for_era5()
        logger.info("... done! Model is initialized and ready to run.")

    def _init_model_for_era5(self):
        """Set up the model for running with ERA-5 initial conditions."""
        model_class = ai_models_shim.get_model_class(self.model_name)
        return model_class(
            # Necessary arguments to instantiate a Model object
            input="cds",
            output="file",
            download_assets=False,
            # Additional arguments. These are generally set as object attributes
            # which are then referred to by various Model methods; unfortunately,
            # they're not clearly declared in the class documentation so there is
            # a bit of trial and error involved in figuring out what's needed.
            assets=config.AI_MODEL_ASSETS_DIR,
            date=int(self.model_init.strftime("%Y%m%d")),
            time=self.model_init.hour,
            lead_time=self.lead_time,
            path=str(self.out_pth),
            metadata={},  # Read by the output data handler
            # Unused arguments that are required by Model class methods to work.
            model_args={},
            assets_sub_directory=None,
            staging_dates=None,
            # TODO: Figure out if we can set up caching of model initial conditions
            # using the default interface.
            archive_requests=False,
            only_gpu=True,
            # Assumed set by GraphcastModel; produces additional auxiliary
            # output NetCDF files.
            debug=False,
        )

    def _init_model_for_gfs(self):
        """Set up the model for running with GFS/GDAS initial conditions."""
        from . import gfs

        model_class = ai_models_shim.get_model_class(self.model_name)

        # Create expected path for processed initial conditions, and check that it's
        # available for us to consume.
        gdas_base_pth = gfs.make_gfs_base_pth(self.model_init)
        gdas_proc_fn = f"gdas.proc-{self.model_name}.grib"
        gdas_proc_pth = gdas_base_pth / gdas_proc_fn
        if not gdas_proc_pth.exists():
            raise RuntimeError(
                f"Expected processed GFS/GDAS initial conditions file not found at"
                f" {gdas_proc_fn}."
            )
        logger.info("Copying processed GFS/GDAS file from cache to local...")
        shutil.copy(gdas_proc_pth, gdas_proc_fn)
        logger.info("... done.")
        logger.info(f"Reading GFS/GDAS initial conditions from {gdas_proc_fn}.")

        return model_class(
            output="file",
            download_assets=False,
            assets=config.AI_MODEL_ASSETS_DIR,
            date=int(self.model_init.strftime("%Y%m%d")),
            time=self.model_init.hour,
            lead_time=self.lead_time,
            path=str(self.out_pth),
            metadata={},
            model_args={},
            assets_sub_directory=None,
            staging_dates=None,
            archive_requests=False,
            only_gpu=True,
            debug=False,
            # The only changes we need to make are how we specify the model input
            # data. We'll use the GFS/GDAS data that we've already prepared - although
            # here we assume the data is available. We can add a sanity check above.
            input="file",
            file=str(gdas_proc_fn),
        )

    @modal.method()
    def run_model(self) -> None:
        logger.info("Invoking AIModel.run_model()...")
        self.init_model.run()


# This routine is made available as a stand-alone function, and it's up to the user
# to ensure that the path config.AI_MODEL_ASSETS_DIR exists and is mapped to the storage
# volume where assets should be cached. We provide this as a stand-alone function so
# that it can be called a cheaper, non-GPU instance and avoid wasting cycles outside
# of model inference on such a more expensive machine.
def _maybe_download_assets(model_name: str) -> None:
    from multiurl import download

    logger.info(f"Maybe retrieving assets for model {model_name}...")

    # For the requested model, retrieve the pretrained model weights and cache them to
    # our storage volume. We are generally replicating the code from
    # ai_models.model.Model.download_assets(), but with some hard-coded options;
    # that method is also originally written as an instance method, and we don't
    # want to run the actual initializer for a model type to access it since
    # that would require us to provide input/output options and otherwise
    # prepare more generally for a model inference run - something we're not
    # ready to do at this stage of setup.
    model_class = ai_models_shim.get_model_class(model_name)
    n_files = len(model_class.download_files)
    n_downloaded = 0
    for i, file in enumerate(model_class.download_files):
        asset = os.path.realpath(os.path.join(config.AI_MODEL_ASSETS_DIR, file))
        if not os.path.exists(asset):
            os.makedirs(os.path.dirname(asset), exist_ok=True)
            logger.info(f"({i}/{n_files}) downloading {asset}")
            download(
                model_class.download_url.format(file=file),
                asset + ".download",
            )
            os.rename(asset + ".download", asset)
            n_downloaded += 1
    if not n_downloaded:
        logger.info("   No assets need to be downloaded.")
    logger.info("... done retrieving assets.")

    template_pth = config.make_gfs_template_path(model_name)
    logger.info("Checking for GFS/GDAS -> ERA-5 template at %s", template_pth)
    if not template_pth.exists():
        logger.info("%s did not exist.", template_pth)
        # Two options: we've saved it to a bucket (so just download it), or we need
        # to generate it from scratch.
        bucket_name = os.environ.get("GCS_BUCKET_NAME", "")
        service_account_info = gcs.get_service_account_json("GCS_SERVICE_ACCOUNT_INFO")
        gcs_handler = gcs.GoogleCloudStorageHandler.with_service_account_info(
            service_account_info
        )
        template_fn = template_pth.name
        target_blob = gcs_handler.client.bucket(bucket_name).blob(template_fn)

        # If the template doesn't exist, call our helper routine that forcibly
        # generates one, for us. Regardless, download from GCS to our local cache
        # afterwards.
        logger.info(
            "Checking for template in GCS bucket gs://%s/%s", bucket_name, template_fn
        )
        if not target_blob.exists():
            logger.info("  Template not found; generating from scratch.")
            make_model_era5_template.local(model_name)

        logger.info(
            "Downloading pre-computed template from gs://%s/%s",
            bucket_name,
            template_fn,
        )
        gcs_handler.download_blob(bucket_name, template_fn, template_pth)


@stub.function(
    image=stub.image,
    secrets=[config.ENV_SECRETS],
    network_file_systems={str(config.CACHE_DIR): volume},
    allow_cross_region_volumes=True,
    timeout=1_800,
)
def generate_forecast(
    model_name: str = "panguweather",
    model_init: datetime.datetime = datetime.datetime(2023, 7, 1, 0, 0),
    lead_time: int = 12,
    use_gfs: bool = False,
    skip_validate_env: bool = False,
    upload_to_gcs: bool = True
):
    """Generate a forecast using the specified model."""

    if not skip_validate_env:
        config.validate_env()

    logger.info(f"Setting up model {model_name} conditions...")
    # Pre-emptively try to download assets from our cheaper CPU-only function, so that
    # we don't waste time on the GPU machine.
    _maybe_download_assets(model_name)
    # If necessary, download and prepare GFS initial conditions. Again, don't waste time
    # with a GPU process for this.
    if use_gfs:
        prepare_gfs_analysis.remote(model_name, model_init)
    ai_model = AIModel(model_name, model_init, lead_time, use_gfs)

    logger.info("Generating forecast...")
    ai_model.run_model.remote()
    logger.info("... forecast complete!")

    # Double check that we successfully produced a model output file.
    logger.info(f"Checking output file {str(ai_model.out_pth)}...")
    if ai_model.out_pth.exists():
        logger.info("   Success!")
    else:
        logger.info("   Did not find expected output file.")

    if upload_to_gcs: 
        # Try to upload to Google Cloud Storage
        bucket_name = os.environ.get("GCS_BUCKET_NAME", "")
        service_account_info = gcs.get_service_account_json("GCS_SERVICE_ACCOUNT_INFO")

        if (bucket_name is None) or (not service_account_info):
            logger.warning("Not able to access to Google Cloud Storage; skipping upload.")
            return

        logger.info(f"Attempting to upload to GCS bucket gs://{bucket_name}...")
        gcs_handler = gcs.GoogleCloudStorageHandler.with_service_account_info(
            service_account_info
        )
        dest_blob_name = ai_model.out_pth.name
        logger.info(f"Uploading to gs://{bucket_name}/{dest_blob_name}")
        gcs_handler.upload_blob(
            bucket_name,
            ai_model.out_pth,
            dest_blob_name,
        )
        logger.info("Checking that upload was successful...")
        target_blob = gcs_handler.client.bucket(bucket_name).blob(dest_blob_name)
        if target_blob.exists():
            logger.info("   Success!")
        else:
            logger.info(
                f"   Did not find expected blob ({dest_blob_name}) in GCS bucket"
                f" ({bucket_name})."
            )
    else:
        logger.warning("Skipping upload to Google Cloud Storage.")


@stub.function(
    image=stub.image,
    secrets=[config.ENV_SECRETS],
    network_file_systems={str(config.CACHE_DIR): volume},
    timeout=7_200,
    allow_cross_region_volumes=True,
)
def make_model_era5_template(model_name: str):
    """Generate a template GRIB file corresponding to the ERA-5 inputs for a given
    AI model."""
    import climetlab as cml
    import numpy as np

    bucket_name = os.environ.get("GCS_BUCKET_NAME", "")
    service_account_info = gcs.get_service_account_json("GCS_SERVICE_ACCOUNT_INFO")
    gcs_handler = gcs.GoogleCloudStorageHandler.with_service_account_info(
        service_account_info
    )

    model_class = ai_models_shim.get_model_class(model_name)
    model = model_class(  # noqa: F811
        # Necessary arguments to instantiate a Model object
        input="cds",
        output="file",
        download_assets=False,
        assets=config.AI_MODEL_ASSETS_DIR,
        date=int(config.DEFAULT_GFS_TEMPLATE_MODEL_EPOCH.strftime("%Y%m%d")),
        time=int(config.DEFAULT_GFS_TEMPLATE_MODEL_EPOCH.strftime("%H")),
        lead_time=6,
        path="_stub.grib2",
        metadata={},
        model_args={},
        assets_sub_directory=None,
        staging_dates=None,
        archive_requests=False,
        only_gpu=False,
        debug=True,
    )

    out_fn = f"{model_name}.input-template.grib2"
    with cml.new_grib_output(out_fn) as f:
        for template in model.input.all_fields:
            f.write(np.zeros_like(template.shape), template=template)

    logger.info("Uploading to gs://%s/%s", bucket_name, out_fn)
    gcs_handler.upload_blob(
        bucket_name,
        out_fn,
        out_fn,
    )
    logger.info("Checking that upload was successful...")
    target_blob = gcs_handler.client.bucket(bucket_name).blob(out_fn)
    if target_blob.exists():
        logger.info("   Success!")
    else:
        logger.info(
            "   Did not find expected blob %s in GCS bucket gs://%s.",
            out_fn,
            bucket_name,
        )


@stub.local_entrypoint()
def main(
    model_name: str = "panguweather",
    lead_time: int = 12,
    model_init: datetime.datetime = datetime.datetime(2023, 7, 1, 0, 0),
    use_gfs: bool = False,
    make_template: bool = False,
    run_checks: bool = False,
    run_forecast: bool = False,
    upload_to_gcs: bool = False
):
    """Entrypoint for triggering a remote ai-models weather forecast run.

    Parameters:
        model: short name for the model to run; must be one of ['panguweather',
            'fourcastnetv2-small', 'graphcast']. Defaults to 'panguweather'.
        lead_time: number of hours to forecast into the future. Defaults to 12.
        model_init: datetime to use when initializing the model. Defaults to
            2023-07-01T00:00.
        use_gfs: use GFS/GDAS initial conditions instead of the default ERA-5
        make_template: generate a template GRIB file corresponding to the ERA-5 inputs
            for a given model.
        run_checks: enable call to remote check_assets() for triaging the application
            runtime environment.
        run_forecast: enable call to remote generate_forecast() for running the actual
            forecast model.
    """
    # Quick sanity checks on model arguments; if we don't need to call out to our
    # remote apps, then we shouldn't!
    if model_name not in ai_models_shim.SUPPORTED_AI_MODELS:
        raise ValueError(
            f"User provided model_name '{model_name}' is not supported; must be one of"
            f" {ai_models_shim.SUPPORTED_AI_MODELS}."
        )

    if make_template:
        make_model_era5_template.remote(model_name)
    if run_checks:
        check_assets.remote()
    if run_forecast:
        generate_forecast.remote(
            model_name=model_name,
            model_init=model_init,
            lead_time=lead_time,
            use_gfs=use_gfs,
            upload_to_gcs=upload_to_gcs
        )
