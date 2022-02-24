# About

A python desktop wrapper around the web-based [excalidraw](https://excalidraw.com) whiteboard free-drawing tool.

My use case is embedding freehand drawings in org-mode notes.

## Building

```shell
$ cd src/rsc/
$ npm install
$ webpack
```

### Dependencies

- `pygobject`: `Gtk`, `Webkit2`
- Python 3 (tested with `python3.10`)

## Usage

The entry point is [src/excalidraw_webview.py](src/excalidraw_webview.py). Running that script will create a fullscreen
Excalidraw Gtk Webview.

### Options

- `--close-on-save`: close the webview immediately when saving the first time (Ctrl+S)
- `file` (positional): the final argument is the filename. It can have one of the following extensions:
  -`.svg`, `.png`: an excalidraw drawing that doubles as an image, and as such be embedded as a file link.
    - `.excalidraw`/anything else: only the actual excalidraw metadata, not redundant image information

# Architecture

The python shell constructs a webview with a [minimal excalidraw app](src/rsc). It defines global functions on `window`
that are invoked by the python code to control Excalidraw (save/load some data). This is like poor man's
message-passing, only I couldn't get the latter to work.