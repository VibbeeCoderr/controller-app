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
android.ndk = 25.1.8937393

[buildozer]
log_level = 2
warn_on_root = 1

[app]
# ... existing config ...

[buildozer]
# Ensure NDK version compatibility
android.ndk = 28c
android.api = 33
android.minapi = 24
android.accept_sdk_license = True

# Add this to force proper header paths
android.useAndroidX = True
android.gradle_dependencies = 

# Critical: Set the correct NDK target
android.ndk_path = 

# Disable problematic optimizations that may trigger header issues
android.add_src = 

# Workaround: Use older Python for Android that's compatible with NDK r28c
p4a.bootstrap = sdl2
p4a.local_recipes = ./recipes
