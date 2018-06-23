import datetime
import os
import traceback
from queue import Empty

import deepdish as dd
import qdarkstyle
from PyQt5.QtCore import QObject

from stytra.calibration import CrossCalibrator
from stytra.collectors import DataCollector
from stytra.stimulation import ProtocolRunner

from stytra.metadata import AnimalMetadata, GeneralMetadata

from stytra.stimulation.stimulus_display import StimulusDisplayWindow

from stytra.gui.container_windows import SimpleExperimentWindow


class Experiment(QObject):
    """General class that runs an experiment.

    Parameters
    ----------
    app : QApplication()
        Application to run the Experiment QObject.
    protocols : list of :class:`Protocol <stytra.stimulation.Protocol>` objects
        list of protocols that can be run in this experiment session.
    directory : str
        (optional) Directory where metadata will be saved. If None, nothing
        will be
        saved (default: None).
    metadata_general: :class:`GeneralMetadata <stytra.metadata.GeneralMetadata>` object
        (optional) Class for saving general metadata about the experiment. I
        If not passed, a default GeneralMetadata object will be set.
    metadata_animal: :class:`AnimalMetadata <stytra.metadata.AnimalMetadata>` object
        (optional) Class for saving animal metadata about the experiment.
        If not passed, a default AnimalMetadata object will be set.
    calibrator : :class:`Calibrator <stytra.calibration.Calibrator>` object
        (optional) Calibrator object to calibrate the stimulus display. If
        not set, a CrossCalibrator will be used.
    asset_directory : str
        (optional) Path where asset files such as movies or images to be
        displayed can be found.
    display_config: dict
        (optional) Dictionary with specifications for the display. Possible
        key values are "full_screen" and "window_size".
    rec_stim_every : int
        (optional) Set to record a movie of the displayed visual stimulus. It
        specifies every how many frames one will be saved (set to 1 to
        record) all displayed frames. The final movie will be saved in the
        directory in an .h5 file.
    trigger : :class:`Trigger <stytra.triggering.Trigger>` object
        (optional) Trigger class to control the beginning of the stimulation.


    """
    def __init__(self,
                 app=None,
                 protocols=None,
                 directory=None,
                 metadata_general=None,
                 metadata_animal=None,
                 calibrator=None,
                 asset_directory='',
                 rec_stim_every=None,
                 display_config=None,
                 trigger=None):
        """ """
        super().__init__()

        self.app = app
        self.app.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())
        self.protocols = protocols
        self.trigger = trigger

        self.asset_dir = asset_directory
        self.directory = directory
        if self.directory is not None:
            if not os.path.isdir(self.directory):
                os.makedirs(self.directory)

        if calibrator is None:
            self.calibrator = CrossCalibrator()
        else:
            self.calibrator = calibrator

        self.window_main = None
        self.scope_config = None

        # to the constructor we need to pass classes, not instances
        # otherwise there are problems because the metadatas are QObjects
        if metadata_general is None:
            self.metadata = GeneralMetadata()
        else:
            self.metadata = metadata_general()

        if metadata_animal is None:
            self.animal_metadata = AnimalMetadata()
        else:
            self.animal_metadata = metadata_animal()

        # We will collect data only of a directory for saving is specified:
        if self.directory is not None:
            self.dc = DataCollector(folder_path=self.directory)
            self.dc.add_param_tree(self.metadata._params)

            # Use the DataCollector object to find the last used protocol, to
            # restore it
            self.last_protocol = \
                self.dc.get_last_value('stimulus_protocol_params')
        else:
            self.dc = None
            self.last_protocol = None

        self.protocol_runner = ProtocolRunner(experiment=self,
                                              protocol=self.last_protocol)

        # assign signals from protocol_runner to be used externally:
        self.sig_protocol_finished = self.protocol_runner.sig_protocol_finished
        self.sig_protocol_started = self.protocol_runner.sig_protocol_started

        self.protocol_runner.sig_protocol_finished.connect(self.end_protocol)

        if display_config is None:
            self.display_config = dict(full_screen=False)
        else:
            self.display_config = display_config

        self.window_display = StimulusDisplayWindow(self.protocol_runner,
                                                    self.calibrator,
                                                    record_stim_every=rec_stim_every)

        if self.display_config.get("window_size", None) is not None:
            self.window_display.params['size'] = self.display_config["window_size"]
            self.window_display.set_dims()

    def start_experiment(self):
        """Start the experiment creating GUI and initialising metadata.

        Parameters
        ----------

        Returns
        -------

        """
        self.make_window()
        self.initialize_metadata()
        self.show_stimulus_screen(self.display_config["full_screen"])
        if self.trigger is not None:
            self.trigger.start()

    def make_window(self):
        """Make experiment GUI, defined in children depending on experiments.
        """
        self.window_main = SimpleExperimentWindow(self)
        self.window_main.show()

    def initialize_metadata(self):
        """Restore parameters from saved config.h5 file.
        """
        # When restoring here data_log to previous values, there may be
        # multiple (one per parameter), calls of functions connected to
        # a change in the params three state.
        # See comment in DataCollector.restore_from_saved()
        if self.dc is not None:
            self.dc.restore_from_saved()

    def show_stimulus_screen(self, full_screen=True):
        """Open window to display the visual stimulus and make it full-screen
        if necessary.

        Parameters
        ----------
        full_screen :
             (Default value = True)

        Returns
        -------

        """
        self.window_display.show()
        if full_screen:
            try:
                self.window_display.windowHandle().setScreen(self.app.screens()[1])
                self.window_display.showFullScreen()
            except IndexError:
                print('Second screen not available')

    def start_protocol(self):
        """Start the protocol from the ProtocolRunner. Before that, send a
        a notification and if required communicate with the microscope to
        synchronize and read configuration.

        Parameters
        ----------

        Returns
        -------

        """
        if self.trigger is not None and self.window_main.chk_scope.isChecked():
            while True:
                if self.trigger.start_event.is_set() and \
                   not self.protocol_runner.running:
                    self.protocol_runner.start()
                    try:
                        self.scope_config = \
                            self.trigger.queue_trigger_params.get(timeout=0.001)

                        if self.dc is not None:
                            self.dc.add_static_data(self.scope_config,
                                                'imaging_microscope_config')
                    except Empty:
                        print('No scope configuration received')
                    break
                else:
                    self.app.processEvents()
        else:
            self.protocol_runner.start()

    def end_protocol(self, save=True):
        """Function called at Protocol end. Reset Protocol and save
        data_log.

        Parameters
        ----------
        save : bool
             Specify whether to save experiment data (Default value = True).

        Returns
        -------

        """
        self.protocol_runner.stop()
        if self.dc is not None:
            self.dc.add_static_data(self.protocol_runner.log, name='stimulus_log')
            self.dc.add_static_data(self.protocol_runner.t_start, name='general_t_protocol_start')
            self.dc.add_static_data(self.protocol_runner.t_end,
                                    name='general_t_protocol_end')

        # TODO saving of dynamic_log should be conditional
        # self.dc.add_data_source(self.protocol_runner.dynamic_log.get_dataframe(),
        #                         name='stimulus_dynamic_log')
        # clean_dict = self.dc.get_clean_dict(paramstree=True)

        if save and self.dc is not None:
            self.dc.save()  # save data_log
            # Save stimulus movie in .h5 file if required:
            movie = self.window_display.widget_display.get_movie()
            if movie is not None:
                movie_dict = dict(movie=movie[0], movie_times=movie[1])
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                dd.io.save(self.directory + '\\' + timestamp +
                           'stim_movie.h5', movie_dict, compression='blosc')
                # movie files can be large, and blosc is orders of magnitude
                # faster

        self.protocol_runner.reset()

    def wrap_up(self, *args, **kwargs):
        """Clean up things before closing gui. Called by close button.

        Parameters
        ----------
        *args :
            
        **kwargs :
            

        Returns
        -------

        """
        if self.protocol_runner is not None:
            self.protocol_runner.timer.stop()
            if self.protocol_runner.protocol is not None:
                self.end_protocol(save=False)
        if self.trigger is not None:
            self.trigger.kill_event.set()
            # self.trigger.join()
            self.trigger.terminate()
        self.app.closeAllWindows()

    def excepthook(self, exctype, value, tb):
        """

        Parameters
        ----------
        exctype :

        value :

        tb :


        Returns
        -------

        """
        traceback.print_tb(tb)
        print('{0}: {1}'.format(exctype, value))
        self.trigger.kill_event.set()
        self.trigger.terminate()
