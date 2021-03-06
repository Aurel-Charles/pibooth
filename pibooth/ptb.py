#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Photobooth main module.
"""

import os
import time
import shutil
import logging
import pygame
import argparse
import os.path as osp
from RPi import GPIO
import pibooth
from pibooth.utils import LOGGER, timeit, PoolingTimer
from pibooth.states import StateMachine, State
from pibooth.view import PtbWindow
from pibooth.config import PtbConfigParser, edit_configuration
from pibooth.controls import camera
from pibooth.pictures.concatenate import generate_picture_from_files
from pibooth.controls.light import PtbLed
from pibooth.controls.button import BUTTON_DOWN, PtbButton
from pibooth.controls.printer import PtbPrinter


class StateWait(State):

    def __init__(self):
        State.__init__(self, 'wait', 'choose')

    def entry_actions(self):
        self.app.window.show_intro(self.app.previous_picture)

    def do_actions(self, events):
        if self.app.find_print_event(events) and self.app.previous_picture_file and self.app.printer.is_installed():
            with timeit("Send final picture to printer"):
                self.app.led_print.blink()
                self.app.printer.print_file(self.app.previous_picture_file)
            time.sleep(0.5)
            self.app.led_print.switch_on()

    def exit_actions(self):
        self.app.led_print.switch_off()

    def validate_transition(self, events):
        if self.app.find_picture_event(events):
            return self.next_name


class StateChoose(State):

    def __init__(self):
        State.__init__(self, 'choose', 'capture')
        self.timer = PoolingTimer(8)

    def entry_actions(self):
        self.app.led_picture.switch_on()
        self.app.max_captures = self.app.config.getint('PICTURE', 'captures')
        self.app.window.show_choice(self.app.max_captures)
        self.timer.start()

    def do_actions(self, events):
        event = self.app.find_choice_event(events)
        if event and self.app.max_captures != 1:
            self.app.max_captures = 1
        elif event and self.app.max_captures != 4:
            self.app.max_captures = 4
        self.app.window.show_choice(self.app.max_captures)

    def validate_transition(self, events):
        if self.timer.is_timeout():
            return self.next_name


class StateCapture(State):

    def __init__(self):
        State.__init__(self, 'capture', 'processing')

    def entry_actions(self):
        LOGGER.info("Start new pictures sequence")
        self.app.previous_picture = None
        self.app.previous_picture_file = None
        self.app.dirname = osp.join(self.app.savedir, time.strftime("%Y-%m-%d-%H-%M-%S"))
        os.makedirs(self.app.dirname)
        self.app.camera.preview(self.app.window.get_rect())

    def do_actions(self, events):
        self.app.led_picture.blink()
        self.app.window.set_picture_number(len(self.app.captures) + 1, self.app.max_captures)

        if self.app.config.getboolean('WINDOW', 'preview_countdown'):
            self.app.window.show_countdown(self.app.config.getint('WINDOW', 'preview_delay'))
        else:
            time.sleep(self.app.config.getint('WINDOW', 'preview_delay'))

        self.app.led_picture.switch_on()
        if self.app.config.getboolean('WINDOW', 'flash'):
            self.app.window.flash(2)

        image_file_name = osp.join(self.app.dirname, "ptb{:03}.jpg".format(len(self.app.captures)))
        with timeit("Take picture and save it in {}".format(image_file_name)):
            self.app.camera.capture(image_file_name)
            self.app.captures.append(image_file_name)

    def exit_actions(self):
        self.app.camera.stop_preview()
        self.app.led_picture.switch_off()

    def validate_transition(self, events):
        if len(self.app.captures) >= self.app.max_captures:
            return self.next_name


class StateProcessing(State):

    def __init__(self):
        State.__init__(self, 'processing', 'print')

    def entry_actions(self):
        self.app.window.show_wait()

        with timeit("Creating merged picture"):
            footer_texts = [self.app.config.get('PICTURE', 'footer_text1'),
                            self.app.config.get('PICTURE', 'footer_text2')]
            bg_color = self.app.config.gettyped('PICTURE', 'bg_color')
            text_color = self.app.config.gettyped('PICTURE', 'text_color')
            self.app.previous_picture = generate_picture_from_files(self.app.captures, footer_texts, bg_color, text_color)

        self.app.previous_picture_file = osp.join(self.app.dirname, time.strftime("%Y-%m-%d-%H-%M-%S") + "_ptb.jpg")
        with timeit("Save the merged picture in {}".format(self.app.previous_picture_file)):
            self.app.previous_picture.save(self.app.previous_picture_file)

    def exit_actions(self):
        self.app.captures = []

    def validate_transition(self, events):
        if self.app.printer.is_installed():
            return self.next_name
        else:
            return 'finish'  # Can not print


class StatePrint(State):

    def __init__(self):
        State.__init__(self, 'print', 'finish')
        self.timer = PoolingTimer(5)
        self.printed = False

    def entry_actions(self):
        with timeit("Display the merged picture"):
            self.app.window.show_print(self.app.previous_picture)
        self.app.led_print.switch_on()
        self.timer.start()

    def do_actions(self, events):
        if self.app.find_print_event(events) and self.app.previous_picture_file:
            with timeit("Send final picture to printer"):
                self.app.led_print.blink()
                self.app.printer.print_file(self.app.previous_picture_file)
            time.sleep(0.5)
            self.app.led_print.switch_on()
            self.printed = True

    def validate_transition(self, events):
        if self.timer.is_timeout() or self.printed:
            return self.next_name


class StateFinish(State):

    def __init__(self):
        State.__init__(self, 'finish', 'wait')

    def entry_actions(self):
        self.app.window.show_finished()
        time.sleep(0.5)


class PtbApplication(object):

    def __init__(self, config):
        self.config = config

        # Clean directory where pictures are saved
        self.savedir = osp.expanduser(config.get('GENERAL', 'directory'))
        if not osp.isdir(self.savedir):
            os.makedirs(self.savedir)
        if osp.isdir(self.savedir) and config.getboolean('GENERAL', 'clear_on_startup'):
            shutil.rmtree(self.savedir)
            os.makedirs(self.savedir)

        # Prepare GPIO, physical pins mode
        GPIO.setmode(GPIO.BOARD)

        # Prepare the pygame module for use
        os.environ['SDL_VIDEO_CENTERED'] = '1'
        pygame.init()
        # Dont catch mouse motion to avoid filling the queue during long actions
        pygame.event.set_blocked(pygame.MOUSEMOTION)

        # Create window of (width, height)
        self.window = PtbWindow(config.gettyped('WINDOW', 'size'))

        self.state_machine = StateMachine(self)
        self.state_machine.add_state(StateWait())
        self.state_machine.add_state(StateChoose())
        self.state_machine.add_state(StateCapture())
        self.state_machine.add_state(StateProcessing())
        self.state_machine.add_state(StatePrint())
        self.state_machine.add_state(StateFinish())

        # Initialize the camera
        if camera.rpi_camera_connected():
            cam_class = camera.RpiCamera
        elif camera.gp_camera_connected():
            cam_class = camera.GpCamera
        else:
            raise EnvironmentError("Neither Pi Camera nor GPhoto2 camera detected")

        self.camera = cam_class(config.getint('CAMERA', 'iso'),
                                config.gettyped('CAMERA', 'resolution'),
                                config.getint('CAMERA', 'rotation'))

        self.led_picture = PtbLed(7)
        self.button_picture = PtbButton(11, config.getfloat('GENERAL', 'debounce_delay'))

        self.led_print = PtbLed(15)
        self.button_print = PtbButton(13, config.getfloat('GENERAL', 'debounce_delay'))

        self.printer = PtbPrinter(config.get('GENERAL', 'printer_name'))

        # Variables shared between states
        self.dirname = None
        self.captures = []
        self.max_captures = None
        self.previous_picture = None
        self.previous_picture_file = None

    def find_quit_event(self, events):
        """Return the event if found in the list.
        """
        for event in events:
            if event.type == pygame.QUIT or\
                    (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                return event

    def find_fullscreen_event(self, events):
        """Return the event if found in the list.
        """
        for event in events:
            if  event.type == pygame.KEYDOWN and\
                    event.key == pygame.K_f and pygame.key.get_mods() & pygame.KMOD_CTRL:
                return event

    def find_picture_event(self, events):
        """Return the event if found in the list.
        """
        for event in events:
            if (event.type == pygame.KEYDOWN and event.key == pygame.K_p) or \
                    (event.type == BUTTON_DOWN and event.pin == self.button_picture):
                return event

    def find_print_event(self, events):
        """Return the event if found in the list.
        """
        for event in events:
            if (event.type == pygame.KEYDOWN and event.key == pygame.K_e and
                    pygame.key.get_mods() & pygame.KMOD_CTRL) or \
                    (event.type == BUTTON_DOWN and event.pin == self.button_print):
                return event

    def find_resize_event(self, events):
        """Return the event if found in the list.
        """
        for event in events:
            if event.type == pygame.VIDEORESIZE:
                return event

    def find_choice_event(self, events):
        """Return the event if found in the list.
        """
        for event in events:
            if (event.type == pygame.KEYDOWN and event.key in (pygame.K_LEFT, pygame.K_RIGHT)) or \
                    (event.type == BUTTON_DOWN and event.pin == self.button_picture):
                return event

    def main_loop(self):
        """Run the main game loop.
        """
        try:
            self.state_machine.set_state('wait')

            while True:
                events = list(reversed(pygame.event.get()))  # Take all events, most recent first

                if self.find_quit_event(events):
                    break

                if self.find_fullscreen_event(events):
                    self.window.toggle_fullscreen()

                event = self.find_resize_event(events)
                if event:
                    self.window.resize(event.size)

                self.state_machine.process(events)

        finally:
            self.led_picture.switch_off()
            self.led_print.switch_off()
            GPIO.cleanup()
            self.camera.quit()
            self.printer.quit()
            pygame.quit()


def main():
    """Application entry point.
    """
    parser = argparse.ArgumentParser(usage="%(prog)s [options]", description=pibooth.__doc__)

    parser.add_argument('--version', action='version', version=pibooth.__version__,
                        help=u"show program's version number and exit")

    parser.add_argument("--config", action='store_true',
                        help=u"edit the current configuration")

    parser.add_argument("--reset", action='store_true',
                        help=u"restore the default configuration")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", "--verbose", dest='logging', action='store_const', const=logging.DEBUG,
                       help=u"report more information about operations", default=logging.INFO)
    group.add_argument("-q", "--quiet", dest='logging', action='store_const', const=logging.WARNING,
                       help=u"report only errors and warnings", default=logging.INFO)

    options, _args = parser.parse_known_args()

    logging.basicConfig(format='[ %(levelname)-8s] %(name)-18s: %(message)s', level=options.logging)

    config = PtbConfigParser("~/.config/pibooth/pibooth.cfg", options.reset)

    if options.config:
        LOGGER.info("Editing the Photo Booth configuration...")
        edit_configuration(config)
    elif not options.reset:
        LOGGER.info("Starting the Photo Booth application...")
        app = PtbApplication(config)
        app.main_loop()


if __name__ == '__main__':
    main()
