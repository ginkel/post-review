import constants
import wx

class AboutBox(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, -1, "About Post Review")
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, wx.ID_ANY, "Post Review - A Review Board Client\nVersion %s\n\nCopyright (C) 2006-2009 Christian Hammond and David Trowbridge. All rights reserved.\n\nSAP DTR Support && GUI by Thilo-Alexander Ginkel." % constants.VERSION), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        wikiUrl = 'https://wiki.wdf.sap.corp/display/RB'
        wikiHyperlink = wx.HyperlinkCtrl(self, wx.ID_ANY, wikiUrl, wikiUrl)
        sizer.Add(wikiHyperlink, flag = wx.ALL & ~(wx.TOP), border = 5)
        button = wx.Button(self, wx.ID_OK)
        button.SetDefault()
        sizer.Add(button, flag = wx.ALL | wx.ALIGN_CENTER, border = 5)

        self.SetSizer(sizer)
        self.SetAutoLayout(1)
        sizer.Fit(self)
        self.Layout()
        
        self.CenterOnParent(wx.BOTH)


class ReviewPostedDialog(wx.Dialog):
    def __init__(self, parent, reviewid, url):
        wx.Dialog.__init__(self, parent, -1, "Review Board")
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, wx.ID_ANY, "Your change has been successfully submitted for review (review request id #%s).\nIt has not yet been published, though. To do so, open the following URL in your web browser and follow the instructions:" % reviewid), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        hyperlink = wx.HyperlinkCtrl(self, wx.ID_ANY, url, url)
        sizer.Add(hyperlink, flag = wx.ALL, border = 5)
        button = wx.Button(self, wx.ID_OK)
        button.SetDefault()
        sizer.Add(button, flag = wx.ALL | wx.ALIGN_CENTER, border = 5)

        self.SetSizer(sizer)
        self.SetAutoLayout(1)
        sizer.Fit(self)
        self.Layout()
        
        self.CenterOnParent(wx.BOTH)


class UpdateAvailableDialog(wx.Dialog):
    def __init__(self, parent, version, url, unsupported):
        wx.Dialog.__init__(self, parent, -1, "Review Board")
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        if unsupported:
            sizer.Add(wx.StaticText(self, wx.ID_ANY, "A new version (%s) of Post Review is available and your current Post Review version %s is too old and thus no longer supported.\n\nTo download and install the updated version, point your web browser to:" % (version, constants.VERSION)), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        else:
            sizer.Add(wx.StaticText(self, wx.ID_ANY, "A new version (%s) of Post Review is available.\n\nTo download and install the updated version, point your web browser to:" % version), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        hyperlink = wx.HyperlinkCtrl(self, wx.ID_ANY, url, url)
        sizer.Add(hyperlink, flag = wx.ALL, border = 5)
        if unsupported:
            sizer.Add(wx.StaticText(self, wx.ID_ANY, "Post Review will exit when you click \"OK\"."), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        button = wx.Button(self, wx.ID_OK)
        button.SetDefault()
        sizer.Add(button, flag = wx.ALL | wx.ALIGN_CENTER, border = 5)

        self.SetSizer(sizer)
        self.SetAutoLayout(1)
        sizer.Fit(self)
        self.Layout()
        
        self.CenterOnParent(wx.BOTH)


class LoginDialog(wx.Dialog):
    def __init__(self, parent, prompt = "Provide your Review Board credentials:", user = '', password = ''):
        wx.Dialog.__init__(self, parent, -1, "Review Board - Login")

        if user is None:
            user = ''
        if password is None:
            password = ''

        self.user = user
        self.password = password

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, wx.ID_ANY, prompt), flag = wx.ALL | wx.ALIGN_LEFT, border = 5)
        
        gridSizer = wx.FlexGridSizer(2, 2, 5, 5)
        gridSizer.Add(wx.StaticText(self, wx.ID_ANY, "&User:"), 0, wx.EXPAND)
        self.id_user = wx.NewId()
        self.userCtrl = wx.TextCtrl(self, self.id_user, user)
        gridSizer.Add(self.userCtrl, 0, wx.EXPAND)
        gridSizer.Add(wx.StaticText(self, wx.ID_ANY, "&Password:"), 0, wx.EXPAND)
        self.id_password = wx.NewId()
        self.passwordCtrl = wx.TextCtrl(self, self.id_password, password, style = wx.TE_PASSWORD)
        gridSizer.Add(self.passwordCtrl, 0, wx.EXPAND)
        sizer.Add(gridSizer, flag = wx.ALL | wx.ALIGN_LEFT, border = 5)

        buttonSizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        if buttonSizer:
            sizer.Add(buttonSizer, flag = wx.ALL | wx.ALIGN_RIGHT, border = 5)

        self.SetSizer(sizer)
        self.SetAutoLayout(1)
        sizer.Fit(self)
        self.Layout()
        
        okButton = self.FindWindowById(wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self.OnOk, okButton)
        
        self.CenterOnParent(wx.BOTH)
        
    def OnOk(self, event):
        self.user = self.userCtrl.GetLabel()
        self.password = self.passwordCtrl.GetLabel()
        self.EndModal(wx.ID_OK)


class PerforceUnavailableDialog(wx.Dialog):
    def __init__(self, parent, config):
        wx.Dialog.__init__(self, parent, -1, "Review Board")

        self.config = config

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, wx.ID_ANY, "Post Review could not locate the Perforce command line client (\"p4.exe\") on your system.\nPerforce support will be unavailable until you install this client program.\n\nYou can download the command line client at:"), flag = wx.RIGHT | wx.LEFT | wx.TOP | wx.ALIGN_LEFT, border = 5)
        p4downloadurl = "http://www.perforce.com/perforce/downloads/index.html"
        hyperlink = wx.HyperlinkCtrl(self, wx.ID_ANY, p4downloadurl, p4downloadurl)
        sizer.Add(hyperlink, flag = wx.RIGHT | wx.LEFT | wx.BOTTOM, border = 5)
        self.dont_bug_me = wx.CheckBox(self, -1, "Do &not show again")
        sizer.Add(self.dont_bug_me, flag = wx.ALL, border = 5)
        okButton = wx.Button(self, wx.ID_OK)
        okButton.SetDefault()
        sizer.Add(okButton, flag = wx.ALL | wx.ALIGN_CENTER, border = 5)
        self.Bind(wx.EVT_BUTTON, self.OnOk, okButton)

        self.SetSizer(sizer)
        self.SetAutoLayout(1)
        sizer.Fit(self)
        self.Layout()
        
        self.CenterOnParent(wx.BOTH)

    def OnOk(self, event):
        self.config.WriteBool(constants.CONFIG_SCM_IGNORE_P4_MISSING, self.dont_bug_me.GetValue())
        self.EndModal(wx.ID_OK)
