
import json
from sys import stdin
import colorama
from colored import fg, bg, attr
from datetime import datetime
from functools import reduce
import socket
from more_itertools import partition
from itertools import tee
import argparse
import threading
from queue import Queue, Empty
from select import select
import time
import pandas as pd
import numpy as np
from bokeh.plotting import figure, curdoc
from tornado import gen
from functools import partial
from bokeh.models import ColumnDataSource
from time import sleep

doc = curdoc()
time_factor = 1
tick = 0.25
period = 60 * 5 * time_factor
x_length = int(period * 0.85)
x_half_length = int(x_length / 2)
highlight_factor = 3

columns = "DateTime,Price,VolumeAtBid,VolumeAtAsk,TotalVolume,BidImbalance,AskImbalance,VolumeDistribution".split(',')
chart_columns = [
    'CellTop',
    'CellBottom',
    'CellLeft',
    'CellRight',
    'CellMiddle',
    'VolAtBidText',
    'Separator',
    'VolAtAskText',
    'VolAtBidColor',
    'VolAtAskColor',
    'TotalVolume',
    'VolumeEnd'
]

def WaitUntilFileReady(filelist):
    rlist, _, _ = select(filelist, [], [])

def ReadOneLine(thefile):

    line = thefile.readline()

    if not line:
        return line

    while line[-1] != '\n':
        sleep(0.5)
        line += thefile.readline()

    return line

def FileReader(thefile):
    while True:
        line = ReadOneLine(thefile)
        if not line:
            return None
        yield line

def SessionReader(thefile):

    reader = FileReader(thefile)
    isFirstLine = True

    for line in reader:
        if not line:
            if isFirstLine:
                return None
            sleep(0.5)
            continue

        if isFirstLine:
            if line == 'SESSION START\n':
                isFirstLine = False
            continue

        if line == 'SESSION END\n':
            return None

        yield line

class Server:

    def ComputeChartParameter(self, table):
        raw_data = pd.DataFrame(table, columns=columns)

        raw_data.DateTime = raw_data.DateTime.astype(np.int64) * time_factor
        CellTop = raw_data.Price.astype(np.float32) + tick
        CellBottom = raw_data.Price.astype(np.float32)
        CellLeft = raw_data.DateTime.astype(np.int64) - x_half_length
        CellRight = raw_data.DateTime.astype(np.int64) + x_half_length
        CellMiddle = raw_data.DateTime.astype(np.int64)
        VolAtBidText = raw_data.VolumeAtBid.astype('string')
        VolAtAskText = raw_data.VolumeAtAsk.astype('string')
        VolAtBidColor = raw_data.BidImbalance.astype(np.float32).apply(
                lambda x: '#000000' if x < highlight_factor else '#FF0000')
        VolAtAskColor = raw_data.AskImbalance.astype(np.float32).apply(
                lambda x: '#000000' if x < highlight_factor else '#00FF00')

        TotalVolume = raw_data.TotalVolume.astype(np.int32)
        VolumeDist = raw_data.VolumeDistribution.astype(np.float32)
        VolumeEnd = CellLeft + VolumeDist * x_length


        chart_parameter = pd.DataFrame({
                'CellTop': CellTop,
                'CellBottom': CellBottom,
                'CellLeft': CellLeft.astype(np.float64),
                'CellRight': CellRight.astype(np.float64),
                'CellMiddle': CellMiddle.astype(np.float64),
                'VolAtBidText': VolAtBidText,
                'Separator': str('x'),
                'VolAtAskText': VolAtAskText,
                'VolAtBidColor': VolAtBidColor.astype('string'),
                'VolAtAskColor': VolAtAskColor.astype('string'),
                'TotalVolume': TotalVolume,
                'VolumeEnd': VolumeEnd.astype(np.float64)
        })

        return chart_parameter

    def plot_source(self, source):
        # plot base
        self.plot.quad(top='CellTop', bottom='CellBottom', left='CellLeft',
                            right='CellRight', color='#F0F0F0', source=source)

        # plot volume profile
        self.plot.quad(top='CellTop', bottom='CellBottom', left='CellLeft',
                            right='VolumeEnd', color='#A0A0A0', source=source)

        # plot bid
        self.plot.text(x='CellMiddle', y='CellBottom', text='VolAtBidText',
                text_color='VolAtBidColor', text_align='right', text_font_size='12px',
                source=source, x_offset=-5)

        # plot x
        self.plot.text(x='CellMiddle', y='CellBottom', text='Separator',
                text_color='#000000', text_align='center', text_font_size='12px',
                source=source)

        # plot ask
        self.plot.text(x='CellMiddle', y='CellBottom', text='VolAtAskText',
                text_color='VolAtAskColor', text_align='left', text_font_size='12px',
                source=source, x_offset=5)


    def __init__(self, rfile, hfile):

        self.rfile = open(rfile)
        self.hfile = open(hfile)

        self.rfile.seek(0, 2)

        table = [line.rstrip().split(',') for line in FileReader(self.hfile)]

        self.hParameters = self.ComputeChartParameter(table)
        self.hsource = ColumnDataSource(self.hParameters)
        self.hIndex = len(table)
        self.rsource = None

        TOOLS = "pan,xwheel_zoom,ywheel_zoom,wheel_zoom,box_zoom,reset,save,crosshair"
        self.plot = figure(tools=TOOLS, x_axis_type = 'datetime')
        self.plot.sizing_mode = 'stretch_both'
        self.plot_source(self.hsource)

        doc.add_root(self.plot)
        doc.on_session_destroyed(self.close)

        self.queue = Queue(maxsize=1)
        self.thread = threading.Thread(target=self.update, daemon=True)
        self.thread.start()

    def close(self, session_context):
        self.rfile.close()
        self.hfile.close()
        pass

    @gen.coroutine
    def update_doc(self):

        hData, rData = self.queue.get()

        if hData['update']:
            self.hsource.data = hData['data']

        if rData['update']:
            if self.rsource == None:
                self.rsource = ColumnDataSource(rData['data'])
                self.plot_source(self.rsource)
            else:
                self.rsource.data = rData['data']

    def update(self):

        try:
            while True:
                if self.rfile.closed or self.hfile.closed:
                    print('rfile or hfile has been closed.')
                    return

                hData = { 'update': False, 'data': None }
                rData = { 'update': False, 'data': None }

                table = [line.rstrip().split(',') for line in FileReader(self.hfile)]
                if len(table) > 0:
                    self.hParameters = self.hParameters.append(self.ComputeChartParameter(table), ignore_index=True)
                    hData['data'] = self.hParameters
                    hData['update'] = True

                latest_table = []
                while True:
                    table = [line.rstrip().split(',') for line in SessionReader(self.rfile)]
                    if len(table) > 0:
                        latest_table = table
                    else:
                        break

                if len(latest_table) > 0:
                    data = self.ComputeChartParameter(latest_table)
                    rData['data'] = data
                    rData['update'] = True

                if len(table) > 0 or len(latest_table) > 0:
                    self.queue.put((hData, rData))

                    # update the document from callback
                    doc.add_next_tick_callback(self.update_doc)

                sleep(0.5)

        except Exception as err:
            print('updater exits due to error ', err)

def Main():
    server = Server('ESZ0-CME-imbalance-5min.rfile', 'ESZ0-CME-imbalance-5min.hfile')

Main()

