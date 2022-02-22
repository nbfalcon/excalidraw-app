import base64
import functools
import json
import os.path
import sys
import types
from enum import IntEnum
from typing import *

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.0')
from gi.repository import Gtk, WebKit2, GLib, Gio, Gdk  # noqa E402


def rsc_path(which_resource):
    return os.path.join(os.path.dirname(__file__), "rsc", which_resource)


# noinspection PyPep8Naming
class g_async:
    def __init__(self, wrap_me):
        self.wrappee = wrap_me

    def __getattr__(self, item: str):
        func = getattr(self.wrappee, item)
        finish = getattr(self.wrappee, item.removesuffix('_async') + '_finish', None)
        if finish is None:
            fst, *components = item.split('_')
            total = fst
            for comp in components:
                new_finish = getattr(self.wrappee, total + '_finish')
                if new_finish is not None:
                    finish = new_finish
                # We don't care about the complete item, since that does not exist
                total += comp

        @types.coroutine
        def async_fn(*args, **kwargs):
            result = yield functools.partial(func, *args, **kwargs)
            return finish(result) if finish is not None else result

        return async_fn

    @staticmethod
    def run(task, callback=None, _initial=None):
        try:
            invoke_me = task.send(_initial)
        except StopIteration as task_result:
            if callback is not None:
                callback(task_result.value)
        else:
            invoke_me(callback=lambda _, result: g_async.run(task, callback, _initial=result))

    @staticmethod
    @types.coroutine
    def promise(cb):
        """Allows invoking callback-based functions in the g_async context. Use like callback_arg = await promise(cb).

        :param cb Called with one keyword argument, resolve. callback should be called with the value await should
        resume with.
        """

        def cb_wrapper(callback):
            cb(resolve=lambda result: callback(None, result))

        resolved = yield cb_wrapper
        return resolved


def _g_async_run_cb(async_fn):
    # A separate function allows debugging _args and _kwargs using a breakpoint
    def cb(*_args, **_kwargs):
        g_async.run(async_fn())

    return cb


class BooleanLock:
    def __init__(self, value):
        self.locked = value

    def __enter__(self):
        self.locked = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.locked = False


def _debug_print(*args):
    if os.getenv("EXCALIDRAW_DEBUG") or "PYCHARM_HOSTED" in os.environ:
        print(*args)


class ExcalidrawSaveFormat(IntEnum):
    JSON = 0
    HYBRID_SVG = 1
    HYBRID_PNG = 2

    def to_js_name(self):
        return {
            ExcalidrawSaveFormat.JSON: "json",
            ExcalidrawSaveFormat.HYBRID_SVG: "svg",
            ExcalidrawSaveFormat.HYBRID_PNG: "png"
        }[self]


class ExcalidrawWindow:
    def __init__(self, open_initially: Optional[str] = None, close_on_save: bool = False):
        window = Gtk.Window()
        window.connect('destroy', Gtk.main_quit)
        window.set_gravity(Gdk.Gravity.CENTER)
        window.set_default_size(800, 600)

        webview = WebKit2.WebView()
        webview.load_uri("file://" + rsc_path("dist/index.html"))
        manager: WebKit2.UserContentManager = webview.get_user_content_manager()
        manager.register_script_message_handler('getSaveData')
        self._get_save_data_nonce = 0
        self._get_save_data_cbs = {}
        manager.connect('script-message-received::getSaveData', self._on_receive_save_data)

        window.add(webview)
        webview.show()

        accels = Gtk.AccelGroup()
        # noinspection PyTypeChecker
        accels.connect(Gdk.KEY_s, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK, Gtk.AccelFlags(0),
                       _g_async_run_cb(self._save_as))
        # noinspection PyTypeChecker
        accels.connect(Gdk.KEY_s, Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags(0), _g_async_run_cb(self._save))
        # noinspection PyTypeChecker
        accels.connect(Gdk.KEY_o, Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags(0), _g_async_run_cb(self._open))
        # noinspection PyTypeChecker
        accels.connect(Gdk.KEY_p, Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags(0), self._print)
        window.add_accel_group(accels)

        self._save_location = None
        self._save_running = BooleanLock(False)
        if open_initially is not None:
            self._open_file(Gio.File.new_for_commandline_arg(open_initially))

        self.close_on_save = close_on_save

        self.webview = webview
        self.window = window

    def _on_receive_save_data(self, _, response: WebKit2.JavascriptResult):
        result_json = json.loads(response.get_js_value().to_json(0))
        data = result_json['data']
        nonce = result_json['nonce']
        self._get_save_data_cbs.pop(nonce)(data)

    # FIXME: export dialog
    # FIXME: debounce
    async def get_save_data(self, save_format: ExcalidrawSaveFormat):
        def cb(resolve):
            # This hack is necessary because unlike in Apple's WebKit, there is no way to run an async javascript
            # function. Our only option is to abuse signal handlers (though this only works if the function
            # finishes quickly enough).
            used_nonce = self._get_save_data_nonce
            self._get_save_data_cbs[used_nonce] = resolve
            args = {'format': save_format.to_js_name(), 'export': False}
            self.webview.run_javascript(
                f"""
                getSaveData({json.dumps(args)}).then(result => 
                    window.webkit.messageHandlers.getSaveData.postMessage(
                        {{data: result, nonce: {used_nonce}}}))
                """)
            # Just for theoretical correctness, ensure we are not losing precision on JS side
            self._get_save_data_nonce = (self._get_save_data_nonce + 1) % 1_000_000

        result = await g_async.promise(cb)
        _debug_print("get_save_data:", result)
        return result

    def _get_save_format(self) -> ExcalidrawSaveFormat:
        if self._save_location is not None:
            uri = self._save_location.get_uri()
            if uri.endswith(".excalidraw.svg"):
                return ExcalidrawSaveFormat.HYBRID_SVG
            elif uri.endswith(".excalidraw.png"):
                return ExcalidrawSaveFormat.HYBRID_PNG
        return ExcalidrawSaveFormat.JSON

    def _load_from(self, data: bytes, save_format: ExcalidrawSaveFormat):
        args = {'format': save_format.to_js_name()}
        if save_format == ExcalidrawSaveFormat.JSON:
            args['data'] = json.loads(data)
        elif save_format == ExcalidrawSaveFormat.HYBRID_SVG:
            args['blob'] = data.decode('utf-8')
        elif save_format == ExcalidrawSaveFormat.HYBRID_PNG:
            args['base64'] = base64.b64encode(data)
        _debug_print("_load_from:", args)
        self.webview.run_javascript(f"""
        (function (args) {{
            if (window.loadSaveData) window.loadSaveData(args);
            else document.addEventListener('DOMContentLoaded', () => {{window.loadSaveData(args);}})
        }})({json.dumps(args)});
        """)

    async def _export_to(self, save_format: ExcalidrawSaveFormat):
        if save_format == ExcalidrawSaveFormat.JSON:
            return json.dumps(await self.get_save_data(ExcalidrawSaveFormat.JSON)).encode('utf-8')
        elif save_format == ExcalidrawSaveFormat.HYBRID_SVG:
            return (await self.get_save_data(ExcalidrawSaveFormat.HYBRID_SVG))['blob'].encode('utf-8')
        elif save_format == ExcalidrawSaveFormat.HYBRID_PNG:
            return base64.b64decode((await self.get_save_data(ExcalidrawSaveFormat.HYBRID_PNG))['base64'])

    async def _perform_save(self):
        if self._save_running.locked:
            return
        with self._save_running:
            assert self._save_location is not None  # Must not call if no save location known
            stream: Gio.FileOutputStream = await g_async(self._save_location).replace_async(
                None, False, Gio.FileCreateFlags.NONE, GLib.PRIORITY_DEFAULT)
            buffer = await self._export_to(self._get_save_format())
            await g_async(stream).write_async(buffer, GLib.PRIORITY_DEFAULT)

    async def _save(self):
        if self._save_location is None:
            await self._save_as()
        else:
            await self._perform_save()
            if self.close_on_save:
                Gtk.main_quit()

    def _make_file_chooser(self, action, ok):
        chooser = Gtk.FileChooserDialog(action=action)
        chooser.set_do_overwrite_confirmation(True)
        chooser.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, ok, Gtk.ResponseType.OK)

        # Using SVG is most convenient, so put it at the top
        svg_filter = Gtk.FileFilter()
        svg_filter.set_name("Excalidraw SVG")
        svg_filter.add_pattern("*.excalidraw.svg")
        chooser.add_filter(svg_filter)
        png_filter = Gtk.FileFilter()
        png_filter.set_name("Excalidraw PNG")
        png_filter.add_pattern("*.excalidraw.png")
        chooser.add_filter(png_filter)
        json_filter = Gtk.FileFilter()
        json_filter.set_name("Excalidraw JSON")
        json_filter.add_pattern("*.excalidraw")
        chooser.add_filter(json_filter)

        if self._save_location is not None:
            chooser.set_uri(self._save_location.get_uri())
        elif action != Gtk.FileChooserAction.OPEN:
            chooser.set_current_name("Untitled.excalidraw.svg")
        return chooser

    async def _set_save_location(self, new_file: Gio.File):
        if self._save_location is not None:
            old_loc = await g_async(self._save_location).query_info_async(Gio.FILE_ATTRIBUTE_ID_FILE,
                                                                          Gio.FileQueryInfoFlags.NONE,
                                                                          GLib.PRIORITY_DEFAULT)
            new_loc = await g_async(new_file).query_info_async(Gio.FILE_ATTRIBUTE_ID_FILE, Gio.FileQueryInfoFlags.NONE,
                                                               GLib.PRIORITY_DEFAULT)
            if old_loc != new_loc:
                self._save_running = BooleanLock(False)
        self._save_location = new_file

    async def _save_as(self):
        chooser = self._make_file_chooser(Gtk.FileChooserAction.SAVE, Gtk.STOCK_SAVE_AS)

        if chooser.run() == Gtk.ResponseType.OK:
            await self._set_save_location(chooser.get_file())
            await self._perform_save()
            self.close_on_save = False
        chooser.destroy()

    async def _open(self):
        chooser = self._make_file_chooser(Gtk.FileChooserAction.OPEN, Gtk.STOCK_OPEN)
        if chooser.run() == Gtk.ResponseType.OK:
            await self._open_file(chooser.get_file())
        chooser.destroy()

    def _print(self, *_):
        print_op = WebKit2.PrintOperation(web_view=self.webview)
        print_op.run_dialog(self.window)

    async def _open_file(self, file: Gio.File):
        await self._set_save_location(file)
        try:
            success, content, *_ = await g_async(self._save_location).load_contents_async()
            if success:
                self._load_from(content, self._get_save_format())
        except GLib.Error as e:
            print(f"Failed to open '{self._save_location.get_uri()}': {e}", file=sys.stderr)

    def show(self):
        self.window.show()


def main():
    Gtk.init(sys.argv)

    import argparse
    argparser = argparse.ArgumentParser(description="Excalidraw webview wrapper")
    argparser.add_argument('file', type=int, nargs='?', help="Which file to open/save to")
    argparser.add_argument('-c', '--close-on-save', action='store_true', dest='cl_save',
                           help="Close when saving normally (Save As disables this)")
    args = argparser.parse_args()

    window = ExcalidrawWindow(open_initially=args.file, close_on_save=args.cl_save)
    window.show()

    Gtk.main()


if __name__ == '__main__':
    main()
