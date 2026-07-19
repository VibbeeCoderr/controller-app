[app]
title = PC Controller
package.name = controllerapp
package.domain = org.yourname

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 0.1

requirements = python3,kivy,plyer

orientation = landscape
fullscreen = 1

android.permissions = INTERNET,VIBRATE

# Start with arm64-v8a only for faster builds; add armeabi-v7a later
# if you need to support older devices.
android.archs = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 1
