import constants
import os
import string
import wx
import wx.lib.intctrl

class PreferencesDialog(wx.Dialog):
    def __init__(self, parent, config, options):
        wx.Dialog.__init__(self, parent, -1, "Preferences", style = wx.DEFAULT_DIALOG_STYLE)

        self.config = config
        self.options = options

        self.prefs_book = wx.Notebook(self, -1)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.prefs_book.AddPage(self.CreateSCMPage(self.prefs_book), "SCM")
        self.prefs_book.AddPage(self.CreateDTRPage(self.prefs_book), "DTR")
        self.prefs_book.AddPage(self.CreatePerforcePage(self.prefs_book), "Perforce")
        sizer.Add(self.prefs_book, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        buttonSizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        if buttonSizer:
            sizer.Add(buttonSizer, flag = wx.ALL | wx.ALIGN_RIGHT, border = 5)

        self.SetSizer(sizer)
        self.Layout()
        self.Fit()
        
        okButton = self.FindWindowById(wx.ID_OK)
        okButton.SetDefault()
        self.Bind(wx.EVT_BUTTON, self.OnOk, okButton)
        
        self.CenterOnParent(wx.BOTH)
        
    def OnOk(self, event):
        self.WriteSCMConfig()
        self.WriteDTRConfig()
        self.WritePerforceConfig()
        self.EndModal(wx.ID_OK)

    def OnCancel(self, event):
        self.EndModal(wx.ID_CANCEL)

    def CreateSCMPage(self, nb):
        panel = wx.Panel(nb, -1)

        sizer = wx.BoxSizer(wx.VERTICAL)

        idOverride = wx.NewId()
        self.override_scmuser = wx.CheckBox(panel, idOverride, "&Override User")
        sizer.Add(self.override_scmuser, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 8)
        wx.EVT_CHECKBOX(self, idOverride, self.OnOverrideClicked)

        user_sizer = wx.BoxSizer(wx.HORIZONTAL)
        user_sizer.Add(wx.StaticText(panel, -1, "&User:"), 0, wx.RIGHT | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)
        self.user_name = wx.TextCtrl(panel)
        user_sizer.Add(self.user_name, 0, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        sizer.Add(user_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        panel.SetSizer(sizer)
        panel.SetAutoLayout(1)
        sizer.Fit(self)

        self.ReadSCMConfig()

        return panel

    def CreateDTRPage(self, nb):
        panel = wx.Panel(nb, -1)

        sizer = wx.BoxSizer(wx.VERTICAL)

        server_sizer = wx.BoxSizer(wx.HORIZONTAL)
        server_sizer.Add(wx.StaticText(panel, -1, "&Server:"), 0, wx.RIGHT | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)
        self.server = wx.TextCtrl(panel, size=(150, -1))
        server_sizer.Add(self.server, 0, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        sizer.Add(server_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.TOP | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        #act_sizer = wx.BoxSizer(wx.HORIZONTAL)
        #act_sizer.Add(wx.StaticText(panel, -1, "&Maximum Activity Age:"), 0, wx.RIGHT | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)
        #self.dtr_cl_age = wx.lib.intctrl.IntCtrl(panel, value = self.config.ReadInt(constants.CONFIG_SCM_MAX_DTR_ACT_AGE, constants.DEFAULT_CONFIG_SCM_MAX_DTR_ACT_AGE), min = 1, limited = True)
        #act_sizer.Add(self.dtr_cl_age, 0, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        #sizer.Add(act_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        panel.SetSizer(sizer)
        panel.SetAutoLayout(1)
        sizer.Fit(self)

        self.ReadDTRConfig()

        return panel

    def CreatePerforcePage(self, nb):
        panel = wx.Panel(nb, -1)

        sizer = wx.BoxSizer(wx.VERTICAL)

        self.show_submitted = wx.CheckBox(panel, -1, "Show &submitted changes")
        sizer.Add(self.show_submitted, 0, wx.ALL | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        cl_sizer = wx.BoxSizer(wx.HORIZONTAL)
        cl_sizer.Add(wx.StaticText(panel, -1, "&Maximum # of CLs:"), 0, wx.RIGHT | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)
        self.p4_cl_count = wx.lib.intctrl.IntCtrl(panel, value = self.config.ReadInt(constants.CONFIG_SCM_MAX_P4_CL_COUNT, constants.DEFAULT_CONFIG_SCM_MAX_P4_CL_COUNT), min = 1, limited = True)
        cl_sizer.Add(self.p4_cl_count, 0, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        sizer.Add(cl_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND | wx.ALIGN_CENTER_VERTICAL, 8)

        panel.SetSizer(sizer)
        panel.SetAutoLayout(1)
        sizer.Fit(self)

        self.ReadPerforceConfig()

        return panel

    def OnOverrideClicked(self, event):
        override = self.override_scmuser.GetValue()
        self.user_name.Enable(override)

    def ReadSCMConfig(self):
        if self.options.scmuser:
            self.override_scmuser.Enable(False)
            self.override_scmuser.SetValue(True)
            self.user_name.Enable(False)
            self.user_name.SetValue(self.options.scmuser)
        else:
            override = self.config.ReadBool(constants.CONFIG_SCM_OVERRIDE_USER, False)
            self.override_scmuser.SetValue(override)
            self.user_name.Enable(override)
            self.user_name.SetValue(self.config.Read(constants.CONFIG_SCM_USER, os.environ['USERNAME']))

    def WriteSCMConfig(self):
        if not self.options.scmuser:
            override = self.override_scmuser.GetValue()
            self.config.WriteBool(constants.CONFIG_SCM_OVERRIDE_USER, override)
            self.config.Write(constants.CONFIG_SCM_USER, self.user_name.GetValue())

    def ReadDTRConfig(self):
        self.server.SetValue(self.config.Read(constants.CONFIG_SCM_DTR_SERVER, constants.DEFAULT_CONFIG_SCM_DTR_SERVER))
        #self.dtr_cl_age.SetValue(self.config.ReadInt(constants.CONFIG_SCM_MAX_DTR_ACT_AGE, constants.DEFAULT_CONFIG_SCM_MAX_DTR_ACT_AGE))

    def WriteDTRConfig(self):
        old_server = self.config.Read(constants.CONFIG_SCM_DTR_SERVER, constants.DEFAULT_CONFIG_SCM_DTR_SERVER)
        new_server = self.server.GetValue()
        self.config.Write(constants.CONFIG_SCM_DTR_SERVER, new_server)

        #self.config.WriteInt(constants.CONFIG_SCM_MAX_DTR_ACT_AGE, self.dtr_cl_age.GetValue())

        if old_server != new_server:
            dlg = RestartRequiredDialog(self)
            dlg.ShowModal()

    def ReadPerforceConfig(self):
        self.p4_cl_count.SetValue(self.config.ReadInt(constants.CONFIG_SCM_MAX_P4_CL_COUNT, constants.DEFAULT_CONFIG_SCM_MAX_P4_CL_COUNT))
        self.show_submitted.SetValue(self.config.ReadBool(constants.CONFIG_SCM_SHOW_SUBMITTED, constants.DEFAULT_CONFIG_SCM_SHOW_SUBMITTED))

    def WritePerforceConfig(self):
        self.config.WriteInt(constants.CONFIG_SCM_MAX_P4_CL_COUNT, self.p4_cl_count.GetValue())
        self.config.WriteBool(constants.CONFIG_SCM_SHOW_SUBMITTED, self.show_submitted.GetValue())


def EditPreferences(parent, config, options):
    dlg = PreferencesDialog(parent, config, options)
    return dlg.ShowModal()

def get_scm_user(config, options):
        if options.scmuser is None:
            if config.ReadBool(constants.CONFIG_SCM_OVERRIDE_USER, False):
                return config.Read(constants.CONFIG_SCM_USER, os.environ['USERNAME'])
            else:
                return string.lower(os.environ['USERNAME'])
        else:
            return options.scmuser

def get_dtr_server(config, options):
    return config.Read(constants.CONFIG_SCM_DTR_SERVER, constants.DEFAULT_CONFIG_SCM_DTR_SERVER)

class RestartRequiredDialog(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, -1, "Post Review - Review Board Client")
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, wx.ID_ANY, "You have made changes to the configuration that require Post Review to be restarted.\n\nAfter closing this dialog, please restart the client so that your changes become effective."), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        button = wx.Button(self, wx.ID_OK)
        button.SetDefault()
        sizer.Add(button, flag = wx.ALL | wx.ALIGN_CENTER, border = 5)

        self.SetSizer(sizer)
        self.SetAutoLayout(1)
        sizer.Fit(self)
        self.Layout()
        
        self.CenterOnParent(wx.BOTH)
