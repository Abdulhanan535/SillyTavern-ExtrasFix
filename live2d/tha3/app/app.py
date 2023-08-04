import argparse
import os
import random
import requests
import sys
import threading
import time
import torch
import io
import torch.nn.functional as F
import wx
import numpy as np

from PIL import Image
from torchvision import transforms
from flask import Flask, Response
from flask_cors import CORS
from io import BytesIO

sys.path.append(os.getcwd())
from tha3.mocap.ifacialmocap_constants import *
from tha3.mocap.ifacialmocap_pose import create_default_ifacialmocap_pose
from tha3.mocap.ifacialmocap_pose_converter import IFacialMocapPoseConverter
from tha3.mocap.ifacialmocap_poser_converter_25 import create_ifacialmocap_pose_converter
from tha3.poser.modes.load_poser import load_poser
from tha3.poser.poser import Poser
from tha3.util import (
    torch_linear_to_srgb, resize_PIL_image, extract_PIL_image_from_filelike,
    extract_pytorch_image_from_PIL_image
)
from typing import Optional

# Global Variables
global_source_image = None
global_result_image = None
global_reload = None
is_talking_override = False
is_talking = False
global_timer_paused = False

# Flask setup
app = Flask(__name__)
CORS(app)

def unload():
    global global_timer_paused
    global_timer_paused = True
    return "Animation Paused"

def start_talking():
    global is_talking_override
    is_talking_override = True
    return "started"

def stop_talking():
    global is_talking_override
    is_talking_override = False
    return "stopped"

def result_feed():
    def generate():
        while True:
            if global_result_image is not None:
                try:
                    rgb_image = global_result_image[:, :, [2, 1, 0]]  # Swap B and R channels
                    pil_image = Image.fromarray(np.uint8(rgb_image))  # Convert to PIL Image
                    if global_result_image.shape[2] == 4: # Check if there is an alpha channel present
                        alpha_channel = global_result_image[:, :, 3] # Extract alpha channel
                        pil_image.putalpha(Image.fromarray(np.uint8(alpha_channel))) # Set alpha channel in the PIL Image
                    buffer = io.BytesIO() # Save as PNG with RGBA mode
                    pil_image.save(buffer, format='PNG')
                    image_bytes = buffer.getvalue()
                except Exception as e:
                    print(f"Error when trying to write image: {e}")
                yield (b'--frame\r\n'  # Send the PNG image
                       b'Content-Type: image/png\r\n\r\n' + image_bytes + b'\r\n')
            else:
                time.sleep(0.1)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

def live2d_load_file(stream):
    global global_source_image
    global global_reload
    global global_timer_paused
    global_timer_paused = False
    try:
        pil_image = Image.open(stream) # Load the image using PIL.Image.open
        img_data = BytesIO() # Create a copy of the image data in memory using BytesIO
        pil_image.save(img_data, format='PNG')
        global_reload = Image.open(BytesIO(img_data.getvalue())) # Set the global_reload to the copy of the image data
    except Image.UnidentifiedImageError:
        print(f"Could not load image from file, loading blank")
        full_path = os.path.join(os.getcwd(), "live2d\\tha3\\images\\inital.png")
        MainFrame.load_image(None, full_path)
        global_timer_paused = True
    return 'OK'

def convert_linear_to_srgb(image: torch.Tensor) -> torch.Tensor:
    rgb_image = torch_linear_to_srgb(image[0:3, :, :])
    return torch.cat([rgb_image, image[3:4, :, :]], dim=0)

def launch_gui(device, model):
    global initAMI
    initAMI = True
    
    parser = argparse.ArgumentParser(description='uWu Waifu')

    # Add other parser arguments here

    args, unknown = parser.parse_known_args()

    try:
        poser = load_poser(model, device)
        pose_converter = create_ifacialmocap_pose_converter()

        app = wx.App(redirect=False)
        main_frame = MainFrame(poser, pose_converter, device)
        main_frame.SetSize((750, 600))

        #Lload default image (you can pass args.char if required)
        full_path = os.path.join(os.getcwd(), "live2d\\tha3\\images\\inital.png")
        main_frame.load_image(None, full_path)

        #main_frame.Show(True)
        main_frame.capture_timer.Start(100)
        main_frame.animation_timer.Start(100)
        wx.DisableAsserts() #prevent popup about debug alert closed from other threads
        app.MainLoop()

    except RuntimeError as e:
        print(e)
        sys.exit()

class MainFrame(wx.Frame):
    def __init__(self, poser: Poser, pose_converter: IFacialMocapPoseConverter, device: torch.device):
        super().__init__(None, wx.ID_ANY, "uWu Waifu")
        self.pose_converter = pose_converter
        self.poser = poser
        self.device = device

        self.image_load_counter = 0
        self.custom_background_image = None  # Add this line

        self.sliders = {}
        self.ifacialmocap_pose = create_default_ifacialmocap_pose()
        self.source_image_bitmap = wx.Bitmap(self.poser.get_image_size(), self.poser.get_image_size())
        self.result_image_bitmap = wx.Bitmap(self.poser.get_image_size(), self.poser.get_image_size())
        self.wx_source_image = None
        self.torch_source_image = None
        self.last_pose = None
        self.last_update_time = None

        self.create_ui()

        self.create_timers()
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.update_source_image_bitmap()
        self.update_result_image_bitmap()

    def create_timers(self):
        self.capture_timer = wx.Timer(self, wx.ID_ANY)
        self.Bind(wx.EVT_TIMER, self.update_capture_panel, id=self.capture_timer.GetId())
        self.animation_timer = wx.Timer(self, wx.ID_ANY)
        self.Bind(wx.EVT_TIMER, self.update_result_image_bitmap, id=self.animation_timer.GetId())

    def on_close(self, event: wx.Event):
        # Stop the timers
        self.animation_timer.Stop()
        self.capture_timer.Stop()

        # Destroy the windows
        self.Destroy()
        event.Skip()
        sys.exit(0)

    def random_generate_value(self, min, max, origin_value):
        random_value = random.choice(list(range(min, max, 1))) / 2500.0
        randomized = origin_value + random_value
        if randomized > 1.0:
            randomized = 1.0
        if randomized < 0:
            randomized = 0
        return randomized

    def animationTalking(self):
        global is_talking
        current_pose = self.ifacialmocap_pose

        # NOTE: randomize mouth
        for blendshape_name in BLENDSHAPE_NAMES:
            if "jawOpen" in blendshape_name:
                if is_talking or is_talking_override:
                    current_pose[blendshape_name] = self.random_generate_value(-5000, 5000, abs(1 - current_pose[blendshape_name]))
                else:
                    current_pose[blendshape_name] = 0

        return current_pose
    
    def animationHeadMove(self):
        current_pose = self.ifacialmocap_pose

        for key in [HEAD_BONE_Y]: #can add more to this list if needed
            current_pose[key] = self.random_generate_value(-20, 20, current_pose[key])
        
        return current_pose
    
    def animationBlink(self):
        current_pose = self.ifacialmocap_pose

        if random.random() <= 0.03:
            current_pose["eyeBlinkRight"] = 1
            current_pose["eyeBlinkLeft"] = 1
        else:
            current_pose["eyeBlinkRight"] = 0
            current_pose["eyeBlinkLeft"] = 0

        return current_pose
    
    def get_emotion_values(self, emotion): # Place to define emotion presets
        emotions = {
            'Happy': {'eyeLookInLeft': 0.0, 'eyeLookOutLeft': 0.0, 'eyeLookDownLeft': 0.0, 'eyeLookUpLeft': 1.0, 'eyeBlinkLeft': 0, 'eyeSquintLeft': 0.0, 'eyeWideLeft': 0.0, 'eyeLookInRight': 0.0, 'eyeLookOutRight': 0.0, 'eyeLookDownRight': 0.0, 'eyeLookUpRight': 0.0, 'eyeBlinkRight': 0, 'eyeSquintRight': 0.0, 'eyeWideRight': 0.0, 'browDownLeft': 0.0, 'browOuterUpLeft': 0.0, 'browDownRight': 0.0, 'browOuterUpRight': 0.0, 'browInnerUp': 0.0, 'noseSneerLeft': 0.0, 'noseSneerRight': 0.0, 'cheekSquintLeft': 0.0, 'cheekSquintRight': 0.0, 'cheekPuff': 0.0, 'mouthLeft': 0.0, 'mouthDimpleLeft': 0.0, 'mouthFrownLeft': 0.0, 'mouthLowerDownLeft': 0.0, 'mouthPressLeft': 0.0, 'mouthSmileLeft': 0.0, 'mouthStretchLeft': 0.0, 'mouthUpperUpLeft': 0.0, 'mouthRight': 0.0, 'mouthDimpleRight': 0.0, 'mouthFrownRight': 0.0, 'mouthLowerDownRight': 0.0, 'mouthPressRight': 0.0, 'mouthSmileRight': 0.0, 'mouthStretchRight': 0.0, 'mouthUpperUpRight': 0.0, 'mouthClose': 0.0, 'mouthFunnel': 0.0, 'mouthPucker': 0.0, 'mouthRollLower': 0.0, 'mouthRollUpper': 0.0, 'mouthShrugLower': 0.0, 'mouthShrugUpper': 0.0, 'jawLeft': 0.0, 'jawRight': 0.0, 'jawForward': 0.0, 'jawOpen': 0, 'tongueOut': 0.0, 'headBoneX': 0.0, 'headBoneY': 0.0, 'headBoneZ': 0.0, 'headBoneQuat': [0.0, 0.0, 0.0, 1.0], 'leftEyeBoneX': 0.0, 'leftEyeBoneY': 0.0, 'leftEyeBoneZ': 0.0, 'leftEyeBoneQuat': [0.0, 0.0, 0.0, 1.0], 'rightEyeBoneX': 0.0, 'rightEyeBoneY': 0.0, 'rightEyeBoneZ': 0.0, 'rightEyeBoneQuat': [0.0, 0.0, 0.0, 1.0]},  
            'Sad': {'eyeLookInLeft': 0.0, 'eyeLookOutLeft': 0.0, 'eyeLookDownLeft': 1.0, 'eyeLookUpLeft': 0.0, 'eyeBlinkLeft': 0, 'eyeSquintLeft': 0.0, 'eyeWideLeft': 0.0, 'eyeLookInRight': 0.0, 'eyeLookOutRight': 0.0, 'eyeLookDownRight': 0.0, 'eyeLookUpRight': 0.0, 'eyeBlinkRight': 0, 'eyeSquintRight': 0.0, 'eyeWideRight': 0.0, 'browDownLeft': 0.0, 'browOuterUpLeft': 0.0, 'browDownRight': 0.0, 'browOuterUpRight': 0.0, 'browInnerUp': 0.0, 'noseSneerLeft': 0.0, 'noseSneerRight': 0.0, 'cheekSquintLeft': 0.0, 'cheekSquintRight': 0.0, 'cheekPuff': 0.0, 'mouthLeft': 0.0, 'mouthDimpleLeft': 0.0, 'mouthFrownLeft': 0.0, 'mouthLowerDownLeft': 0.0, 'mouthPressLeft': 0.0, 'mouthSmileLeft': 0.0, 'mouthStretchLeft': 0.0, 'mouthUpperUpLeft': 0.0, 'mouthRight': 0.0, 'mouthDimpleRight': 0.0, 'mouthFrownRight': 0.0, 'mouthLowerDownRight': 0.0, 'mouthPressRight': 0.0, 'mouthSmileRight': 0.0, 'mouthStretchRight': 0.0, 'mouthUpperUpRight': 0.0, 'mouthClose': 0.0, 'mouthFunnel': 0.0, 'mouthPucker': 0.0, 'mouthRollLower': 0.0, 'mouthRollUpper': 0.0, 'mouthShrugLower': 0.0, 'mouthShrugUpper': 0.0, 'jawLeft': 0.0, 'jawRight': 0.0, 'jawForward': 0.0, 'jawOpen': 0, 'tongueOut': 0.0, 'headBoneX': 0.0, 'headBoneY': 0.0, 'headBoneZ': 0.0, 'headBoneQuat': [0.0, 0.0, 0.0, 1.0], 'leftEyeBoneX': 0.0, 'leftEyeBoneY': 0.0, 'leftEyeBoneZ': 0.0, 'leftEyeBoneQuat': [0.0, 0.0, 0.0, 1.0], 'rightEyeBoneX': 0.0, 'rightEyeBoneY': 0.0, 'rightEyeBoneZ': 0.0, 'rightEyeBoneQuat': [0.0, 0.0, 0.0, 1.0]},
            'Angry': {'eyeLookInLeft': 1.0, 'eyeLookOutLeft': 1.0, 'eyeLookDownLeft': 1.0, 'eyeLookUpLeft': 1.0, 'eyeBlinkLeft': 0, 'eyeSquintLeft': 0.0, 'eyeWideLeft': 0.0, 'eyeLookInRight': 0.0, 'eyeLookOutRight': 0.0, 'eyeLookDownRight': 0.0, 'eyeLookUpRight': 0.0, 'eyeBlinkRight': 0, 'eyeSquintRight': 0.0, 'eyeWideRight': 0.0, 'browDownLeft': 0.0, 'browOuterUpLeft': 0.0, 'browDownRight': 0.0, 'browOuterUpRight': 0.0, 'browInnerUp': 0.0, 'noseSneerLeft': 0.0, 'noseSneerRight': 0.0, 'cheekSquintLeft': 0.0, 'cheekSquintRight': 0.0, 'cheekPuff': 0.0, 'mouthLeft': 0.0, 'mouthDimpleLeft': 0.0, 'mouthFrownLeft': 0.0, 'mouthLowerDownLeft': 0.0, 'mouthPressLeft': 0.0, 'mouthSmileLeft': 0.0, 'mouthStretchLeft': 0.0, 'mouthUpperUpLeft': 0.0, 'mouthRight': 0.0, 'mouthDimpleRight': 0.0, 'mouthFrownRight': 0.0, 'mouthLowerDownRight': 0.0, 'mouthPressRight': 0.0, 'mouthSmileRight': 0.0, 'mouthStretchRight': 0.0, 'mouthUpperUpRight': 0.0, 'mouthClose': 0.0, 'mouthFunnel': 0.0, 'mouthPucker': 0.0, 'mouthRollLower': 0.0, 'mouthRollUpper': 0.0, 'mouthShrugLower': 0.0, 'mouthShrugUpper': 0.0, 'jawLeft': 0.0, 'jawRight': 0.0, 'jawForward': 0.0, 'jawOpen': 0, 'tongueOut': 0.0, 'headBoneX': 0.0, 'headBoneY': 0.0, 'headBoneZ': 0.0, 'headBoneQuat': [0.0, 0.0, 0.0, 1.0], 'leftEyeBoneX': 0.0, 'leftEyeBoneY': 0.0, 'leftEyeBoneZ': 0.0, 'leftEyeBoneQuat': [0.0, 0.0, 0.0, 1.0], 'rightEyeBoneX': 0.0, 'rightEyeBoneY': 0.0, 'rightEyeBoneZ': 0.0, 'rightEyeBoneQuat': [0.0, 0.0, 0.0, 1.0]},
        }
        return emotions.get(emotion, {})

    def animationMain(self): 
        self.ifacialmocap_pose =  self.animationBlink()
        self.ifacialmocap_pose =  self.animationHeadMove()
        self.ifacialmocap_pose =  self.animationTalking()
        return self.ifacialmocap_pose

    def on_erase_background(self, event: wx.Event):
        pass

    def create_animation_panel(self, parent):
        self.animation_panel = wx.Panel(parent, style=wx.RAISED_BORDER)
        self.animation_panel_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.animation_panel.SetSizer(self.animation_panel_sizer)
        self.animation_panel.SetAutoLayout(1)

        image_size = self.poser.get_image_size()

        # Left Column (Image)
        self.animation_left_panel = wx.Panel(self.animation_panel, style=wx.SIMPLE_BORDER)
        self.animation_left_panel_sizer = wx.BoxSizer(wx.VERTICAL)
        self.animation_left_panel.SetSizer(self.animation_left_panel_sizer)
        self.animation_left_panel.SetAutoLayout(1)
        self.animation_panel_sizer.Add(self.animation_left_panel, 1, wx.EXPAND)

        self.result_image_panel = wx.Panel(self.animation_left_panel, size=(image_size, image_size),
                                           style=wx.SIMPLE_BORDER)
        self.result_image_panel.Bind(wx.EVT_PAINT, self.paint_result_image_panel)
        self.result_image_panel.Bind(wx.EVT_ERASE_BACKGROUND, self.on_erase_background)
        self.result_image_panel.Bind(wx.EVT_LEFT_DOWN, self.load_image)
        self.animation_left_panel_sizer.Add(self.result_image_panel, 1, wx.EXPAND)

        separator = wx.StaticLine(self.animation_left_panel, -1, size=(256, 1))
        self.animation_left_panel_sizer.Add(separator, 0, wx.EXPAND)

        self.animation_left_panel_sizer.Fit(self.animation_left_panel)

        # Right Column (Sliders)

        self.animation_right_panel = wx.Panel(self.animation_panel, style=wx.SIMPLE_BORDER)
        self.animation_right_panel_sizer = wx.BoxSizer(wx.VERTICAL)
        self.animation_right_panel.SetSizer(self.animation_right_panel_sizer)
        self.animation_right_panel.SetAutoLayout(1)
        self.animation_panel_sizer.Add(self.animation_right_panel, 1, wx.EXPAND)

        separator = wx.StaticLine(self.animation_right_panel, -1, size=(256, 5))
        self.animation_right_panel_sizer.Add(separator, 0, wx.EXPAND)

        background_text = wx.StaticText(self.animation_right_panel, label="--- Background ---", style=wx.ALIGN_CENTER)
        self.animation_right_panel_sizer.Add(background_text, 0, wx.EXPAND)

        self.output_background_choice = wx.Choice(
            self.animation_right_panel,
            choices=[
                "TRANSPARENT",
                "GREEN",
                "BLUE",
                "BLACK",
                "WHITE",
                "LOADED",
                "CUSTOM"
            ]
        )
        self.output_background_choice.SetSelection(0)
        self.animation_right_panel_sizer.Add(self.output_background_choice, 0, wx.EXPAND)


        #self.pose_converter.init_pose_converter_panel(self.animation_panel) # this changes sliders to breathing on

        #sliders go here


        blendshape_groups = {
            'Eyes': ['eyeLookOutLeft', 'eyeLookOutRight', 'eyeLookDownLeft', 'eyeLookUpLeft', 'eyeWideLeft', 'eyeWideRight'],
            'Mouth': ['mouthFrownLeft'],
            'Cheek': ['cheekSquintLeft', 'cheekSquintRight', 'cheekPuff'],
            'Brow': ['browDownLeft', 'browOuterUpLeft', 'browDownRight', 'browOuterUpRight', 'browInnerUp'],
            'Eyelash': ['mouthSmileLeft'],
            'Nose': ['noseSneerLeft', 'noseSneerRight'],
            'Misc': ['tongueOut']
        }

        for group_name, variables in blendshape_groups.items():
            collapsible_pane = wx.CollapsiblePane(self.animation_right_panel, label=group_name, style=wx.CP_DEFAULT_STYLE | wx.CP_NO_TLW_RESIZE)
            collapsible_pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self.on_pane_changed)
            self.animation_right_panel_sizer.Add(collapsible_pane, 0, wx.EXPAND)
            pane_sizer = wx.BoxSizer(wx.VERTICAL)
            collapsible_pane.GetPane().SetSizer(pane_sizer)

            for variable in variables:
                variable_label = wx.StaticText(collapsible_pane.GetPane(), label=variable)

                # Multiply min and max values by 100 for the slider
                slider = wx.Slider(
                    collapsible_pane.GetPane(),
                    value=0,
                    minValue=0,
                    maxValue=100,
                    size=(150, -1),  # Set the width to 150 and height to default
                    style=wx.SL_HORIZONTAL | wx.SL_LABELS
                )

                slider.SetName(variable)
                slider.Bind(wx.EVT_SLIDER, self.on_slider_change)
                self.sliders[slider.GetId()] = slider

                pane_sizer.Add(variable_label, 0, wx.ALIGN_CENTER | wx.ALL, 5)
                pane_sizer.Add(slider, 0, wx.EXPAND)

        self.animation_right_panel_sizer.Fit(self.animation_right_panel)
        self.animation_panel_sizer.Fit(self.animation_panel)

    def on_pane_changed(self, event):
        # Update the layout when a collapsible pane is expanded or collapsed
        self.animation_right_panel.Layout()

    def on_slider_change(self, event):
        slider = event.GetEventObject()
        value = slider.GetValue() / 100.0  # Divide by 100 to get the actual float value
        #print(value)
        slider_name = slider.GetName()
        self.ifacialmocap_pose[slider_name] = value

    def create_ui(self):
        #MAke the UI Elements
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.main_sizer)
        self.SetAutoLayout(1)

        self.capture_pose_lock = threading.Lock()

        #Main panel with JPS
        self.create_animation_panel(self)
        self.main_sizer.Add(self.animation_panel, wx.SizerFlags(0).Expand().Border(wx.ALL, 5))

    def update_capture_panel(self, event: wx.Event):
        data = self.ifacialmocap_pose
        for rotation_name in ROTATION_NAMES:
            value = data[rotation_name]

    @staticmethod
    def convert_to_100(x):
        return int(max(0.0, min(1.0, x)) * 100)

    def paint_source_image_panel(self, event: wx.Event):
        wx.BufferedPaintDC(self.source_image_panel, self.source_image_bitmap)

    def update_source_image_bitmap(self):
        dc = wx.MemoryDC()
        dc.SelectObject(self.source_image_bitmap)
        if self.wx_source_image is None:
            self.draw_nothing_yet_string(dc)
        else:
            dc.Clear()
            dc.DrawBitmap(self.wx_source_image, 0, 0, True)
        del dc

    def draw_nothing_yet_string(self, dc):
        dc.Clear()
        font = wx.Font(wx.FontInfo(14).Family(wx.FONTFAMILY_SWISS))
        dc.SetFont(font)
        w, h = dc.GetTextExtent("Nothing yet!")
        dc.DrawText("Nothing yet!", (self.poser.get_image_size() - w) // 2, (self.poser.get_image_size() - h) // 2)

    def paint_result_image_panel(self, event: wx.Event):
        wx.BufferedPaintDC(self.result_image_panel, self.result_image_bitmap)

    def update_result_image_bitmap(self, event: Optional[wx.Event] = None):
        global global_timer_paused
        global initAMI
        global global_result_image
        global global_reload

        if global_timer_paused:
            return

        try:

            if global_reload is not None:
                MainFrame.load_image(self, event=None, file_path=None)  # call load_image function here
                return

            ifacialmocap_pose = self.animationMain() #GET ANIMATION CHANGES
            current_pose = self.pose_converter.convert(ifacialmocap_pose)

            if self.last_pose is not None and self.last_pose == current_pose:
                return
            
            self.last_pose = current_pose

            if self.torch_source_image is None:
                dc = wx.MemoryDC()
                dc.SelectObject(self.result_image_bitmap)
                self.draw_nothing_yet_string(dc)
                del dc
                return

            pose = torch.tensor(current_pose, device=self.device, dtype=self.poser.get_dtype())

            with torch.no_grad():
                output_image = self.poser.pose(self.torch_source_image, pose)[0].float()
                output_image = convert_linear_to_srgb((output_image + 1.0) / 2.0)

                c, h, w = output_image.shape
                output_image = (255.0 * torch.transpose(output_image.reshape(c, h * w), 0, 1)).reshape(h, w, c).byte()


            numpy_image = output_image.detach().cpu().numpy()
            wx_image = wx.ImageFromBuffer(numpy_image.shape[0],
                                        numpy_image.shape[1],
                                        numpy_image[:, :, 0:3].tobytes(),
                                        numpy_image[:, :, 3].tobytes())
            wx_bitmap = wx_image.ConvertToBitmap()

            dc = wx.MemoryDC()
            dc.SelectObject(self.result_image_bitmap)
            dc.Clear()
            dc.DrawBitmap(wx_bitmap,
                        (self.poser.get_image_size() - numpy_image.shape[0]) // 2,
                        (self.poser.get_image_size() - numpy_image.shape[1]) // 2, True)

            numpy_image_bgra = numpy_image[:, :, [2, 1, 0, 3]] # Convert color channels from RGB to BGR and keep alpha channel
            global_result_image = numpy_image_bgra

            del dc

            if(initAMI == True): #If the models are just now initalized stop animation to save
                global_timer_paused = True
                initAMI = False

            self.Refresh()

        except KeyboardInterrupt:
            print("Update process was interrupted by the user.")
            wx.Exit()

    def resize_image(image, size=(512, 512)):
        image.thumbnail(size, Image.LANCZOS)  # Step 1: Resize the image to maintain the aspect ratio with the larger dimension being 512 pixels
        new_image = Image.new("RGBA", size)   # Step 2: Create a new image of size 512x512 with transparency
        new_image.paste(image, ((size[0] - image.size[0]) // 2,
                                (size[1] - image.size[1]) // 2))   # Step 3: Paste the resized image into the new image, centered
        return new_image

    def load_image(self, event: wx.Event, file_path=None):

        global global_source_image  # Declare global_source_image as a global variable
        global global_reload

        if global_reload is not None:
            file_path = "global_reload"

        try:   
            if file_path == "global_reload":
                pil_image = global_reload 
            else:
                pil_image = resize_PIL_image(
                    extract_PIL_image_from_filelike(file_path),
                    (self.poser.get_image_size(), self.poser.get_image_size()))

            w, h = pil_image.size

            if pil_image.size != (512, 512):
                print("Resizing Char Card to work")
                pil_image = MainFrame.resize_image(pil_image)

            w, h = pil_image.size

            if pil_image.mode != 'RGBA':
                self.source_image_string = "Image must have alpha channel!"
                self.wx_source_image = None
                self.torch_source_image = None
            else:
                self.wx_source_image = wx.Bitmap.FromBufferRGBA(w, h, pil_image.convert("RGBA").tobytes())
                self.torch_source_image = extract_pytorch_image_from_PIL_image(pil_image) \
                    .to(self.device).to(self.poser.get_dtype())

            global_source_image = self.torch_source_image  # Set global_source_image as a global variable

            self.update_source_image_bitmap()

        except Exception as error:
            print("Error: ", error)

        global_reload = None #reset the globe load
        self.Refresh()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='uWu Waifu')
    parser.add_argument(
        '--model',
        type=str,
        required=False,
        default='separable_float',
        choices=['standard_float', 'separable_float', 'standard_half', 'separable_half'],
        help='The model to use.'
    )
    parser.add_argument('--char', type=str, required=False, help='The path to the character image.')
    parser.add_argument(
        '--device',
        type=str,
        required=False,
        default='cuda',
        choices=['cpu', 'cuda'],
        help='The device to use for PyTorch ("cuda" for GPU, "cpu" for CPU).'
    )

    args = parser.parse_args()
    launch_gui(device=args.device, model=args.model)