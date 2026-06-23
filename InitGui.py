import os
import sys

import FreeCAD as App
import FreeCADGui as Gui


# Create the command class
class FreeCADMCPShowCommand:
    """Command to show the FreeCAD MCP panel"""

    def GetResources(self):
        user_dir = App.getUserAppDataDir()
        icon_path = os.path.join(user_dir, "Mod", "freecad_mcp", "assets", "icon.svg")
        return {
            "Pixmap": icon_path,
            "MenuText": "Show FreeCAD MCP Panel",
            "ToolTip": "Show the FreeCAD Model Control Protocol panel",
        }

    def IsActive(self):
        return True

    def Activated(self):
        import freecad_mcp

        freecad_mcp.show_panel()


# Register the command
if not hasattr(Gui, "freecad_mcp_command"):
    Gui.freecad_mcp_command = FreeCADMCPShowCommand()
    Gui.addCommand("FreeCAD_MCP_Show", Gui.freecad_mcp_command)


class FreeCADMCPWorkbench(Gui.Workbench):
    MenuText = "FreeCAD MCP"
    ToolTip = "FreeCAD Model Control Protocol"

    def GetIcon(self):
        """Return the icon for this workbench"""
        user_dir = App.getUserAppDataDir()
        icon_path = os.path.join(user_dir, "Mod", "freecad_mcp", "assets", "icon.svg")
        return icon_path

    def Initialize(self):
        """This function is called at workbench creation."""
        # Add current directory to Python path
        mod_dir = os.path.join(App.getUserAppDataDir(), "Mod", "freecad_mcp")
        if mod_dir not in sys.path:
            sys.path.append(mod_dir)

        # List of commands in the workbench
        self.command_list = ["FreeCAD_MCP_Show"]

        # Create the toolbar
        self.appendToolbar("FreeCAD MCP Tools", self.command_list)

        # Create the menu
        self.appendMenu("&FreeCAD MCP", self.command_list)

    def Activated(self):
        """This function is called when the workbench is activated."""
        pass

    def Deactivated(self):
        """This function is called when the workbench is deactivated."""
        pass

    def GetClassName(self):
        """Return the name of the associated C++ class."""
        return "Gui::PythonWorkbench"


# Add the workbench if it hasn't been added already
if not hasattr(Gui, "freecad_mcp_workbench"):
    Gui.freecad_mcp_workbench = FreeCADMCPWorkbench()
    Gui.addWorkbench(Gui.freecad_mcp_workbench)
