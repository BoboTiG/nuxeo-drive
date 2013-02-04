"""URL scheme / protocol event listener for the OSX platform

The URL scheme association itself is defined in the Info.plist file of the .app
bundle generated by running the `python setup.py py2app` command.

The following just ensures that the currently executed bundle will be used by
default in case several applications are registered for the nxdrive:// URL
scheme which is very unlikely.

The fact that the nxedit:// URL is passed as a commandline argument instead of
a OSX runtime event is achieved thanks to the custom QApplication subclass to
explicitly deal with URL related events unders OSX.

"""
from nxdrive.logging_config import get_logger
log = get_logger(__name__)

NXDRIVE_SCHEME = 'nxdrive'


def register_protocol_handlers(controller):
    """Register the URL scheme listener using PyObjC"""
    try:
        from Foundation import NSBundle
        from LaunchServices import LSSetDefaultHandlerForURLScheme
    except ImportError:
        log.warning("Cannot register %r scheme: missing OSX Foundation module",
                    NXDRIVE_SCHEME)
        return

    bundle_id = NSBundle.mainBundle().bundleIdentifier()
    if bundle_id == 'org.python.python':
        log.debug("Skipping URL scheme registration as this program "
                  " was launched from the Python OSX app bundle")
        return
    LSSetDefaultHandlerForURLScheme(NXDRIVE_SCHEME, bundle_id)
    log.debug("Registered bundle '%s' for URL scheme '%s'", bundle_id,
              NXDRIVE_SCHEME)
