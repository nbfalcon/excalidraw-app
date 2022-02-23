import Excalidraw, { exportToSvg, exportToBlob, loadFromBlob, serializeAsJSON } from '@excalidraw/excalidraw';
import { useRef } from 'react';

export default function App() {
  /**
   * @type import('@excalidraw/excalidraw/types/types').ExcalidrawAPIRefValue
   */
  const excalidraw = useRef(null);
  const render = (
    <div className="excalidraw-wrapper">
      <Excalidraw
        ref={excalidraw}
        UIOptions={{ canvasActions: { clearCanvas: false, export: false, loadScene: false, saveAsImage: false, saveToActiveFile: true } }}>
      </Excalidraw>
    </div>
  );
  window.getSaveData = async ({format, 'export': exportMode}) => {
    const extra = {exportEmbedScene: exportMode ? undefined : true};
    switch (format) {
      case "json":
        const serialized = JSON.parse(serializeAsJSON(excalidraw.current.getSceneElements(), excalidraw.current.getAppState()));
        serialized.source = undefined;
        return serialized;
      case "svg":
        return { blob: (await exportToSvg({
          elements: excalidraw.current.getSceneElements(),
          appState: {...excalidraw.current.getAppState(), ...extra}
        })).outerHTML };
      case "png":
        const blob = await exportToBlob({
          elements: excalidraw.current.getSceneElements(),
          appState: {...excalidraw.current.getAppState(), ...extra}
        });
        const base64 = await new Promise((resolve) => {
          const reader = new FileReader();
          reader.readAsDataURL(blob);
          reader.onloadend = () => {
            resolve(reader.result);
          }
        });
        return { base64: base64 };
      default:
        alert(`Invalid save format ${format} requested. This is a bug`);
        return null;
    }
  };
  window.loadSaveData = async (data) => {
    switch (data.format) {
      case "json":
        excalidraw.current.updateScene(data.data);
        break;
      case "svg":
      case "png":
        /**
         * @type Blob
         */
        let blob;
        if (data.format === "svg") {
          blob = new Blob([data.blob], {type: 'image/svg+xml'});
        }
        else {
          blob = await (await fetch(`data:image/png;base64,${data.base64}`)).blob();
        }
        excalidraw.current.updateScene(await loadFromBlob(blob, excalidraw.current.getAppState(), excalidraw.current.getSceneElements()));
        break;
      default:
        alert(`Invalid load format ${data.format} requested. This is a bug`);
        return null;
    }
  }
  return render;
}
