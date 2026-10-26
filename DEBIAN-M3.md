# Postcard nativo en Debian 13 (MacBook Pro M3 Max, Hyprland)

El código es el de upstream sin cambios. Postcard pide libadwaita 1.8 (`Adw.ShortcutsDialog`) y libadwaita 1.8 pide
GTK ≥ 4.19.4; Debian 13 trae GTK 4.18 y libadwaita 1.7, y los paquetes de sid exigen una glibc más nueva. Solución:
GTK 4.20.4 + libadwaita 1.8.8 compilados en un prefijo privado `/opt/gtk420` que solo usa Postcard, y Postcard
instalado en ese prefijo con un lanzador en `/usr/local/bin/postcard` que fija `LD_LIBRARY_PATH`, `GI_TYPELIB_PATH`,
`XDG_DATA_DIRS` y `PYTHONPATH`.

Detalles que cuestan tiempo si no se saben:
- GTK se compila **con** backend X11 aunque la sesión sea Wayland: el `libwebkitgtk-6.0` de Debian referencia
  símbolos `gdk_x11_*` y sin ellos no carga (`undefined symbol: gdk_x11`), y Postcard se queda sin vista HTML.
- No tener ningún venv de Python/meson en el `PATH` al configurar: meson toma ese Python y el lanzador sale sin PyGObject.
- `blueprint-compiler` necesita ver el typelib de libadwaita 1.8 (`GI_TYPELIB_PATH`) para compilar `shortcuts-dialog.blp`.

La receta completa está en el repo del sistema: `waybian/parches/postcard/compilar-gtk420-postcard.sh`.
