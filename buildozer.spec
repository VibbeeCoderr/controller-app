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
android.archs = arm64-v8a

[android]
android.accept_sdk_license = True
android.min_api = 24
android.api = 33
android.ndk = 27.2.12479018
android.skip_update = True
android.useAndroidX = True

[buildozer]
log_level = 2
warn_on_root = 1
