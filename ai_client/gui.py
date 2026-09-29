import json
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog
from .config import ROOT, load, save
from .device import AndroidDevice
from .policy import PolicyProcess
from .control import AutoController
from .pipeline import PipelinePanel
from .model_input import INPUT_LABELS
from .trial import NativeModelTrial
from .autonomous import AutonomousBattle
from .matches import MatchLedger
from .observability import EventJournal, export_report
from .battle_view import BattleView
from .debug_arena import DebugArena
from .continuous.geometry import emulator_window, marker_to_android, target_pixels
from .continuous.overlay import FloatingTargets
from .continuous.series import ContinuousSeries, MAX_MATCHES

CARD_ROWS=json.loads(Path(load()['card_names_config']).read_text())['cards']
CARD_NAMES={int(row['id']):row['zh'] for row in CARD_ROWS}

class App:
    def __init__(self, root):
        self.root = root
        root.title('BAKA HARNESS')
        root.geometry('680x470')
        root.minsize(620, 450)
        self.config = load()
        self.events = queue.Queue()
        self.device = AndroidDevice(self.config)
        self.policy = PolicyProcess(self.config)
        self.controller = AutoController(self.config, self.policy, self.device, self.emit)
        self.trial = NativeModelTrial(self.policy, self.device, self.emit)
        self.autonomous = AutonomousBattle(self.config,self.policy,self.device,self.emit)
        self.ledger = MatchLedger(self.config['match_directory'])
        self.journal = EventJournal(ROOT / 'records/app-events.jsonl', card_names=CARD_NAMES)
        self.log_rows = deque(maxlen=500)
        self.image = self.photo = None
        self.calibration_step = None
        self.corner = None
        self.decision_count = 0
        self.auto_checked_world = None
        self.last_stream_at = None
        self.stream_stale = False
        self.last_capture_tick = None
        self.last_capture_lag = None
        self.last_inference_ms = None
        self.preview_width, self.preview_height = 180, 320
        self.busy = False
        self.launch_mode = None
        self.variables = {}
        self.status = {}
        self.floating_targets = None
        self.series = ContinuousSeries(
            arm=lambda: self._arm_match(False), tap=self.perform_series_tap,
            stop_match=self.stop_series_match, schedule=root.after,
            status=self.set_series_status)
        self.build()
        root.report_callback_exception = self.callback_exception
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Escape>', lambda event: self.stop())
        root.bind('<F6>', lambda event: self.connect())
        root.bind('<F7>', lambda event: self.load_model())
        root.bind('<F8>', lambda event: self.model_demo())
        root.bind('<F9>', lambda event: self.check_model_input())
        root.bind('<F10>', lambda event: self.model_trial())
        root.bind('<F11>', lambda event: self.start())
        root.after(100, self.drain)

    def emit(self, event, value):
        self.events.put((event, value))

    def work(self, fn):
        if self.busy:
            self.emit('error', '上一项操作尚未完成')
            return
        self.busy = True
        def run():
            try:
                fn()
            except Exception as error:
                self.emit('exception', dict(message=str(error),operation=getattr(fn,'__name__','worker'),
                    traceback=traceback.format_exc()))
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def callback_exception(self, exc_type, error, tb):
        self.emit('exception', dict(message=str(error),operation='gui_callback',
            traceback=''.join(traceback.format_exception(exc_type,error,tb))))

    def build(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#eef3f8')
        style.configure('TLabel', background='#eef3f8', foreground='#243c59')
        style.configure('TLabelframe', background='#eef3f8', bordercolor='#d4dfeb')
        style.configure('TLabelframe.Label', background='#eef3f8', foreground='#243c59')
        style.configure('TButton', padding=(9, 7), font=('Helvetica', 11))
        style.configure('TCheckbutton', background='#eef3f8')
        style.configure('Treeview', rowheight=27, font=('Helvetica', 10))
        style.configure('Title.TLabel', font=('Helvetica', 22, 'bold'))
        style.configure('Step.TLabel', font=('Helvetica', 11, 'bold'))
        style.configure('State.TLabel', font=('Helvetica', 15, 'bold'))
        style.configure('Primary.TButton', padding=(20, 11), font=('Helvetica', 13, 'bold'),
                        background='#1d70b7', foreground='white')
        style.map('Primary.TButton', background=[('active', '#135b9a')])
        style.configure('Mode.TButton', padding=(20, 11), font=('Helvetica', 13, 'bold'),
                        background='#e1eaf3', foreground='#243c59')
        style.map('Mode.TButton', background=[('active', '#cbdbe9')])
        outer = self.home_page = ttk.Frame(self.root, padding=12)
        outer.pack(fill='both', expand=True)
        title_bar=ttk.Frame(outer)
        title_bar.pack(fill='x')
        ttk.Label(title_bar, text='你说的对但是必有一锤大人来了', style='Title.TLabel').pack(side='left')
        ttk.Button(title_bar,text='⚙',width=3,command=self.open_settings).pack(side='right')
        ttk.Label(outer, text='经典 1v1 由模型观察战场并直接下牌', foreground='#556579').pack(anchor='w', pady=(2, 8))
        controls = ttk.LabelFrame(outer, text='模型与对局', padding=8)
        controls.pack(fill='x', pady=(0, 8))
        setup = ttk.Frame(controls)
        setup.pack(fill='x')
        self.model_choice = tk.StringVar(value=self.checkpoint_label(self.config['checkpoint']))
        ttk.Label(setup,text='模型文件',style='Step.TLabel').pack(side='left')
        ttk.Entry(setup,textvariable=self.model_choice,state='readonly',width=29).pack(side='left',padx=(10,8))
        ttk.Button(setup,text='选择 .pt',command=self.choose_checkpoint).pack(side='left')
        self.optimistic_label=tk.StringVar()
        self.refresh_optimistic_label()
        ttk.Button(setup,textvariable=self.optimistic_label,command=self.toggle_optimistic_entities).pack(side='right')
        self.dry = tk.BooleanVar(value=False)
        setup_actions = ttk.Frame(controls)
        setup_actions.pack(fill='x', pady=(8, 0))
        ttk.Button(setup_actions,text='连接设备',command=self.connect).pack(side='left')
        ttk.Button(setup_actions,text='加载模型',command=self.load_model).pack(side='left',padx=(8,0))
        self.card_gap_label=tk.StringVar(value=f"下牌间隔 {self.config.get('min_card_gap_ms',1000)} ms")
        ttk.Button(setup_actions,textvariable=self.card_gap_label,command=self.open_card_gap_settings).pack(side='right')
        actions = ttk.Frame(controls)
        actions.pack(fill='x', pady=(8, 0))
        self.single_button=ttk.Button(actions,text='单次对战',style='Mode.TButton',width=10,command=self.start)
        self.single_button.pack(side='left')
        self.continuous_button=ttk.Button(actions,text='连续对战',style='Mode.TButton',width=10,command=self.start_continuous)
        self.continuous_button.pack(side='left',padx=(8,0))
        self.series_count=tk.StringVar(value=str(self.config.get('continuous_count',3)))
        self.series_count_spinbox=ttk.Spinbox(actions,from_=1,to=MAX_MATCHES,textvariable=self.series_count,width=5)
        self.series_count_spinbox.pack(side='left',padx=(6,0))
        ttk.Label(actions,text='场').pack(side='left',padx=(3,0))
        ttk.Button(actions,text='停止',command=self.stop).pack(side='left',padx=(12,0))
        self.series_status=tk.StringVar(value='未运行')
        ttk.Label(controls,textvariable=self.series_status,foreground='#536778',wraplength=640).pack(anchor='w',pady=(8,0))
        ttk.Label(controls,text='单次对战请手动进入经典 1v1；连续对战会按设置中的 Android 按钮映射自动点击。',
            foreground='#556579',wraplength=640).pack(anchor='w',pady=(3,0))
        overview = ttk.LabelFrame(outer, text='当前状态', padding=8)
        overview.pack(fill='x',pady=(0,8))
        self.headline = tk.StringVar(value='等待连接设备和加载模型')
        ttk.Label(overview,textvariable=self.headline,style='State.TLabel',wraplength=700).pack(anchor='w')
        quick = ttk.Frame(overview)
        quick.pack(fill='x',pady=(8,0))
        self.status['device'] = tk.StringVar(value='未连接')
        self.status['model'] = tk.StringVar(value='未加载')
        ttk.Label(quick,text='设备：').pack(side='left')
        ttk.Label(quick,textvariable=self.status['device'],wraplength=250).pack(side='left',padx=(0,16))
        ttk.Label(quick,text='模型：').pack(side='left')
        ttk.Label(quick,textvariable=self.status['model'],wraplength=250).pack(side='left')
        navigation = ttk.Frame(outer)
        navigation.pack(fill='x', pady=(6, 0))
        ttk.Button(navigation, text='对局记录', command=self.open_battle).pack(side='left')
        ttk.Button(navigation, text='状态与报错', command=self.open_status).pack(side='left', padx=(8, 0))
        ttk.Button(navigation, text='事件记录', command=self.open_logs).pack(side='left', padx=(8, 0))
        ttk.Button(navigation, text='调试战场', command=self.open_debug).pack(side='left', padx=(8, 0))
        self.page_host=ttk.Frame(self.root,padding=12)
        header=ttk.Frame(self.page_host)
        header.pack(fill='x',pady=(0,8))
        ttk.Button(header,text='← 返回',command=self.go_back).pack(side='left')
        self.page_title=tk.StringVar(value='')
        ttk.Label(header,textvariable=self.page_title,style='State.TLabel').pack(side='left',padx=14)
        self.page_body=ttk.Frame(self.page_host)
        self.page_body.pack(fill='both',expand=True)
        self.battle_view = BattleView(self.page_body, CARD_NAMES)
        self.debug_arena = DebugArena(self.page_body, CARD_NAMES)
        self.card_page=ttk.Frame(self.page_body,padding=12)
        ttk.Label(self.card_page,text='中文名称与游戏 ID · '+self.config['card_names_config'],
            wraplength=850).pack(anchor='w',pady=(0,8))
        card_table=ttk.Treeview(self.card_page,columns=('id','zh','en'),show='headings')
        for column,title,width in [('id','游戏 ID',150),('zh','中文名称',200),('en','英文名称',220)]:
            card_table.heading(column,text=title)
            card_table.column(column,width=width,stretch=column!='id')
        for card in CARD_ROWS:
            card_table.insert('', 'end',values=(card['id'],card['zh'],card['en']))
        card_scroll=ttk.Scrollbar(self.card_page,orient='vertical',command=card_table.yview)
        card_table.configure(yscrollcommand=card_scroll.set)
        card_table.pack(side='left',fill='both',expand=True)
        card_scroll.pack(side='right',fill='y')
        diagnostics_tab=self.status_page=ttk.Frame(self.page_body,padding=12)
        self.pipeline = PipelinePanel(diagnostics_tab)
        self.pipeline.pack(fill='x',pady=(0,12))
        states = ttk.LabelFrame(diagnostics_tab, text='详细状态', padding=10)
        states.pack(fill='x')
        self.last_error = tk.StringVar(value='最近错误：无')
        for name, label, value in [('device', '设备', '未连接'), ('probe', 'Overlay 探针', '未连接'),
                ('model', '模型', '未加载'), ('battle', '对局', '等待'),
                ('coverage', '模型输入', '等待实时输入检查'),
                ('native', '内部下牌', '经典 1v1 原生部署与实际应用已验证'),
                ('control', '托管', '已停止')]:
            row = ttk.Frame(states)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=label, width=12).pack(side='left')
            variable = self.status.get(name)
            if variable is None:
                variable = tk.StringVar(value=value)
                self.status[name] = variable
            ttk.Label(row, textvariable=variable, wraplength=650).pack(side='left')
        ttk.Label(diagnostics_tab,textvariable=self.last_error,foreground='#b83b3b',wraplength=850).pack(anchor='w',pady=(8,0))
        logs = self.logs_page = ttk.Frame(self.page_body,padding=8)
        settings_tab=self.settings_page=ttk.Frame(self.page_body)
        settings_canvas=self.settings_canvas=tk.Canvas(settings_tab,highlightthickness=0,bg='#eef3f8')
        scrollbar=ttk.Scrollbar(settings_tab,orient='vertical',command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y');settings_canvas.pack(side='left',fill='both',expand=True)
        settings=ttk.Frame(settings_canvas,padding=8)
        window=settings_canvas.create_window(0,0,window=settings,anchor='nw')
        settings.bind('<Configure>',lambda e:settings_canvas.configure(scrollregion=settings_canvas.bbox('all')))
        settings_canvas.bind('<Configure>',lambda e:settings_canvas.itemconfigure(window,width=e.width))
        settings_canvas.bind('<MouseWheel>',lambda e:settings_canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        self.root.bind('<F3>',lambda event:self.open_settings())
        self.root.bind('<F4>',lambda event:self.open_logs())
        self.root.bind('<F5>',lambda event:self.open_battle())
        self.input_hint = tk.StringVar(value='等待实时对局')
        self.live_metrics = tk.StringVar(value='采集 tick —  · 传输 —  · 推理 —  · 待应用 0')
        self.decision_text = tk.StringVar(value='模型尚未输出动作。')
        self.application_text=tk.StringVar(value='客户端实际下牌：尚无')
        log_tools=ttk.Frame(logs)
        log_tools.pack(fill='x',padx=8,pady=6)
        ttk.Label(log_tools,text='筛选').pack(side='left')
        self.log_filter=tk.StringVar(value='全部')
        filter_box=ttk.Combobox(log_tools,textvariable=self.log_filter,state='readonly',width=8,
            values=('全部','错误','告警','设备','模型','下牌','对局'))
        filter_box.pack(side='left',padx=6);filter_box.bind('<<ComboboxSelected>>',lambda event:self.render_logs())
        ttk.Button(log_tools,text='导出诊断报告',command=self.export_diagnostics).pack(side='right')
        self.report_path=tk.StringVar(value='应用事件：records/app-events.jsonl')
        ttk.Label(logs,textvariable=self.report_path,wraplength=690,foreground='#536778').pack(anchor='w',padx=8,pady=(0,4))
        self.log = tk.Text(logs, wrap='word', font=('Menlo', 11), bg='#101c2d', fg='#d7e5f5',
                           insertbackground='white', relief='flat', state='disabled')
        self.log.pack(fill='both', expand=True)
        self.log.tag_configure('error',foreground='#ff9090')
        self.log.tag_configure('warning',foreground='#f3c56b')
        self.variables['checkpoint']=tk.StringVar(value=str(self.config['checkpoint']))
        self.min_card_gap_var=tk.StringVar(value=str(self.config.get('min_card_gap_ms',1000)))
        self.optimistic_var=tk.BooleanVar(value=self.config.get('optimistic_entities',True))
        ttk.Label(settings,text='最小下牌间隔').grid(row=0,column=0,sticky='w',pady=6)
        self.card_gap_spinbox=ttk.Spinbox(settings,from_=0,to=5000,increment=100,
            textvariable=self.min_card_gap_var,width=9)
        self.card_gap_spinbox.grid(row=0,column=1,sticky='w',padx=8)
        ttk.Label(settings,text='ms · 0 为关闭；只限制两次下牌提交，英雄技能不受限。',
            foreground='#536778').grid(row=1,column=0,columnspan=3,sticky='w',pady=(0,8))
        mapping=ttk.LabelFrame(settings,text='连续对战 · Android 按钮映射',padding=8)
        mapping.grid(row=2,column=0,columnspan=3,sticky='ew',pady=(3,10))
        point_row=ttk.Frame(mapping)
        point_row.pack(fill='x')
        ttk.Button(point_row,text='显示并拖动按钮',command=self.show_targets).pack(side='left')
        ttk.Button(point_row,text='隐藏定位按钮',command=self.hide_targets).pack(side='left',padx=8)
        self.series_targets_text=tk.StringVar(value='')
        ttk.Label(mapping,textvariable=self.series_targets_text,wraplength=700).pack(anchor='w',pady=(8,3))
        ttk.Label(mapping,text='将绿色浮标拖到游戏“开始对局”，橙色浮标拖到结算页“结束/确定”；保存 Android 点击位置。',
            foreground='#536778',wraplength=700).pack(anchor='w')
        self.refresh_series_targets()
        settings_fields=[('adb', 'ADB 路径'), ('serial', '模拟器地址'),
                ('package', '腾讯包名'), ('expected_version', '支持版本'), ('frida_host', 'Frida 地址'),
                ('frida_server', 'Frida Server'),
                ('model_directory', '模型存放文件夹'), ('match_directory', 'JSONL 存放文件夹'),
                ('card_names_config', '卡牌中文/ID 对照表'),
                ('firstlight', 'FirstLight 源码'), ('frame_directory', '完整观测目录')]
        for index, (key, label) in enumerate(settings_fields):
            row=index+3
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky='w', pady=6)
            var = tk.StringVar(value=str(self.config[key]))
            if key != 'card_names_config':
                self.variables[key] = var
            else:
                self.card_names_path_var = var
            ttk.Entry(settings, textvariable=var, width=45,
                state='readonly' if key=='card_names_config' else 'normal').grid(row=row, column=1, sticky='ew', padx=8)
            if key in ('adb', 'frida_server', 'model_directory', 'match_directory'):
                ttk.Button(settings, text='选择', command=lambda k=key: self.browse(k)).grid(row=row, column=2)
            elif key=='card_names_config':
                ttk.Button(settings,text='查看',command=self.open_cards).grid(row=row,column=2)
        settings.columnconfigure(1, weight=1)
        extra_row=len(settings_fields)+3
        ttk.Button(settings, text='保存设置', command=self.save_settings).grid(row=extra_row, column=1, sticky='w', pady=10)
        ttk.Label(settings, text='真实战场数据保持原样；待应用单位仅作为模型输入中的临时预测。',
                  wraplength=500).grid(row=extra_row+1, column=0, columnspan=3, sticky='w')
        diagnostics=ttk.Frame(settings)
        diagnostics.grid(row=extra_row+2,column=0,columnspan=3,sticky='w',pady=10)
        for text,command in [('检查输入',self.check_model_input),('离线推理',self.model_demo),('模型单次试验',self.model_trial),('原生单次测试',self.native_test)]:
            ttk.Button(diagnostics,text=text,command=command).pack(side='left',padx=3)
        ttk.Checkbutton(settings,text='仅推理，不向客户端下牌',variable=self.dry).grid(row=extra_row+3,column=0,columnspan=3,sticky='w',pady=8)
        ttk.Checkbutton(settings,text='待应用预测单位：提交后先让模型看到防守单位',
            variable=self.optimistic_var).grid(row=extra_row+4,column=0,columnspan=3,sticky='w',pady=8)
        self.canvas = None
        self.flip = tk.BooleanVar(value=self.config['flip'])
        self.hint = tk.StringVar(value='网格供预览；内部下牌使用原生坐标，无需选择卡牌。')
        self.add_log('经典 1v1 原生连续下牌已验证。比赛中只运行一个模型，不并行模型回放。')
        self.refresh_battle_controls()

    def show_home(self):
        self.page_host.pack_forget()
        self.home_page.pack(fill='both',expand=True)
        self.root.minsize(620,450)
        self.root.geometry('680x470')

    def go_back(self):
        if getattr(self,'back_target','home') == 'settings':
            self.open_settings()
        else:
            self.show_home()

    def show_page(self,page,title,width,height,back='home'):
        self.home_page.pack_forget()
        for child in (self.battle_view.frame,self.card_page,
                      self.status_page,self.logs_page,self.settings_page,self.debug_arena.frame):
            child.pack_forget()
        self.page_title.set(title)
        self.back_target=back
        self.page_host.pack(fill='both',expand=True)
        page.pack(fill='both',expand=True)
        self.root.minsize(min(width,720),min(height,480))
        self.root.geometry(f'{width}x{height}')

    def open_battle(self):
        self.show_page(self.battle_view.frame,'对局记录',900,700)

    def open_debug(self):
        self.show_page(self.debug_arena.frame,'调试战场',740,740)
        self.root.after_idle(self.debug_arena.render)

    def open_status(self):
        self.show_page(self.status_page,'状态与报错',940,490)

    def open_logs(self):
        self.show_page(self.logs_page,'事件记录',850,560)

    def open_settings(self):
        self.show_page(self.settings_page,'设置',820,620)

    def open_card_gap_settings(self):
        self.open_settings()
        self.settings_canvas.yview_moveto(0)
        self.card_gap_spinbox.focus_set()
        self.card_gap_spinbox.select_range(0,'end')

    def refresh_optimistic_label(self):
        enabled = bool(self.config.get('optimistic_entities',True))
        self.optimistic_label.set('虚拟实体：开' if enabled else '虚拟实体：关')

    def toggle_optimistic_entities(self):
        enabled = not bool(self.config.get('optimistic_entities',True))
        self.config['optimistic_entities'] = enabled
        self.optimistic_var.set(enabled)
        self.refresh_optimistic_label()
        save(self.config)
        self.add_log('虚拟实体已' + ('开启；后续下牌将生成投影' if enabled else '关闭；下一次决策起不再使用投影'))

    def open_cards(self):
        self.show_page(self.card_page,'卡牌中文名称与游戏 ID',820,640,back='settings')

    def refresh_series_targets(self):
        targets=self.config.get('continuous_targets') or {}
        def label(kind, title):
            target=targets.get(kind)
            if not target:return title+'：未定位'
            x,y=target_pixels(target,(self.config['screen_width'],self.config['screen_height']))
            return f'{title}：Android ({x}, {y})'
        self.series_targets_text.set(label('start','开始按钮')+'    ·    '+label('end','结束按钮'))

    def show_targets(self):
        if self.launch_mode is not None or self.series.active or self.autonomous.running():
            self.emit('error','请先停止对战再拖动按钮位置');return
        if not self.device.connected:
            self.emit('error','请先连接设备，再定位游戏按钮');return
        if self.floating_targets is None:
            self.floating_targets=FloatingTargets(self.root,self.config.get('continuous_targets') or {},
                                                   self.on_target_drop)
        self.floating_targets.show()

    def hide_targets(self):
        if self.floating_targets is not None:self.floating_targets.hide()

    def on_target_drop(self, kind, center):
        try:
            if not self.device.connected:raise RuntimeError('请先连接设备')
            target=marker_to_android(center,emulator_window(),
                                     (self.config['screen_width'],self.config['screen_height']))
            self.config.setdefault('continuous_targets',{})[kind]=target
            save(self.config)
            self.refresh_series_targets()
            self.set_series_status(('开始' if kind=='start' else '结束')+'按钮位置已保存')
        except Exception as error:
            self.emit('error','按钮定位失败：'+str(error))

    def set_series_status(self, message):
        self.series_status.set(message)
        if not self.series.active and self.launch_mode == 'continuous':
            self.launch_mode = None
        self.refresh_battle_controls()
        self.emit('continuous_status',message)

    def refresh_battle_controls(self):
        locked = (self.launch_mode is not None or self.series.active or
                  self.autonomous.running() or self.trial.running())
        visual_state=(self.launch_mode,locked)
        if getattr(self,'_battle_controls_state',None)==visual_state:
            return
        self._battle_controls_state=visual_state
        self.single_button.configure(state='disabled' if locked else 'normal',
            style='Primary.TButton' if self.launch_mode == 'single' else 'Mode.TButton')
        self.continuous_button.configure(state='disabled' if locked else 'normal',
            style='Primary.TButton' if self.launch_mode == 'continuous' else 'Mode.TButton')
        self.series_count_spinbox.configure(state='disabled' if locked else 'normal')

    def stop_series_match(self):
        self.autonomous.stop()
        def cancel():
            try:self.device.cancel_native()
            except Exception as error:self.emit('error','取消原生命令失败：'+str(error))
        threading.Thread(target=cancel,daemon=True).start()

    def perform_series_tap(self, kind, target):
        def click():
            x=y=None;error=None
            try:
                if not self.device.connected or not self.device.foreground():
                    raise RuntimeError('腾讯客户端未在 Android 前台')
                x,y=target_pixels(target,(self.config['screen_width'],self.config['screen_height']))
                self.device.adb('shell','input','tap',x,y)
            except Exception as exc:
                error=str(exc)
            self.emit('continuous_tap_result',dict(kind=kind,x=x,y=y,error=error))
        threading.Thread(target=click,daemon=True).start()

    def start_continuous(self):
        try:
            if self.launch_mode is not None or self.series.active:
                raise RuntimeError('已有对战在运行，请先停止或等待本局结束')
            if self.busy or self.autonomous.running() or self.trial.running():
                raise RuntimeError('请等待当前操作结束后启动连战')
            if self.dry.get():raise RuntimeError('连战需要在设置中关闭“仅推理”')
            if not self.device.connected or not self.policy.process or self.policy.process.poll() is not None:
                raise RuntimeError('请先连接设备并加载模型')
            count=int(self.series_count.get())
            if not 1<=count<=MAX_MATCHES:raise ValueError(f'对战次数必须是 1～{MAX_MATCHES} 的整数')
            targets=self.config.get('continuous_targets') or {}
            for kind in ('start','end'):
                if kind in targets:
                    target_pixels(targets[kind],(self.config['screen_width'],self.config['screen_height']))
            self.config['continuous_count']=count
            save(self.config)
            self.hide_targets()
            self.launch_mode='continuous'
            self.series.begin(count,targets)
            self.refresh_battle_controls()
        except (ValueError,RuntimeError,KeyError) as error:
            if not self.series.active and self.launch_mode == 'continuous':
                self.launch_mode = None
                self.refresh_battle_controls()
            self.emit('error',str(error))

    def browse(self, key):
        if key in ('model_directory','match_directory'):
            current=Path(self.variables[key].get())
            path=filedialog.askdirectory(title='选择'+('模型' if key=='model_directory' else '对局 JSONL')+'文件夹',
                initialdir=str(current if current.is_dir() else ROOT))
        else:
            path = filedialog.askopenfilename()
        if path:
            self.variables[key].set(path)

    def checkpoint_label(self, checkpoint):
        return Path(checkpoint).name

    def choose_checkpoint(self):
        if self.busy or self.autonomous.running() or self.trial.running() or self.series.active:
            self.emit('error','等待当前操作完成或停止对战后才能切换模型')
            return
        current = Path(self.variables['model_directory'].get())
        path = filedialog.askopenfilename(title='选择 FirstLight .pt 模型文件',
            initialdir=str(current if current.is_dir() else ROOT/'models'),
            filetypes=[('PyTorch 模型', '*.pt')])
        if not path:
            return
        chosen = Path(path).expanduser().resolve()
        if chosen.suffix.lower() != '.pt' or not chosen.is_file():
            self.emit('error','请选择存在的 .pt 模型文件')
            return
        previous = self.variables['checkpoint'].get()
        previous_directory = self.variables['model_directory'].get()
        self.variables['checkpoint'].set(str(chosen))
        self.variables['model_directory'].set(str(chosen.parent))
        if not self.save_settings():
            self.variables['checkpoint'].set(previous)
            self.variables['model_directory'].set(previous_directory)
            return
        self.model_choice.set(self.checkpoint_label(str(chosen)))
        self.headline.set(f'已选择 {chosen.name}；请点击“加载模型”')
        self.emit('ui_action',dict(name='select_model_file',checkpoint=str(chosen)))

    def save_settings(self):
        try:
            card_gap_ms=int(self.min_card_gap_var.get().strip())
        except ValueError:
            self.emit('error','最小下牌间隔须为 0–5000 的整数毫秒');return False
        if not 0<=card_gap_ms<=5000:
            self.emit('error','最小下牌间隔须为 0–5000 的整数毫秒');return False
        if self.series.active:
            self.emit('error','请先停止对战再修改设置');return False
        if self.autonomous.running():
            self.emit('error','请先停止对战再修改设置');return False
        if self.trial.running():
            self.emit('error', '请先停止 FL 实战试验')
            return False
        if self.controller.thread and self.controller.thread.is_alive():
            self.emit('error', '请先停止托管再修改设置')
            return False
        changed = {k for k,var in self.variables.items() if self.config[k] != var.get().strip()}
        if changed.intersection({'checkpoint','firstlight'}):
            self.policy.close()
            self.status['model'].set('模型设置已变更，请重新加载')
        if changed.difference({'checkpoint','firstlight','frame_directory',
                               'model_directory','match_directory'}):
            self.device.close()
            self.status['device'].set('设备设置已变更，请重新连接')
        for key, variable in self.variables.items():
            self.config[key] = variable.get().strip()
        if 'checkpoint' in changed:
            self.model_choice.set(self.checkpoint_label(self.config['checkpoint']))
        if 'match_directory' in changed:
            self.ledger=MatchLedger(self.config['match_directory'])
        self.config['flip'] = self.flip.get()
        self.config['min_card_gap_ms']=card_gap_ms
        self.config['optimistic_entities']=bool(self.optimistic_var.get())
        self.card_gap_label.set(f'下牌间隔 {card_gap_ms} ms')
        self.refresh_optimistic_label()
        save(self.config)
        self.add_log('设置已保存')
        return True

    def connect(self):
        if not self.save_settings():
            return
        self.device.close()
        self.emit('ui_action',dict(name='connect_device'))
        self.headline.set('正在连接设备…')
        self.status['device'].set('连接中…')
        self.pipeline.set('device', 'active', '连接 ADB 与原生探针')
        def connect():
            pid = self.device.connect(self.on_probe)
            self.emit('connected', pid)
        self.work(connect)

    def on_probe(self, payload):
        self.controller.overlay_event(payload)
        self.emit('probe', payload)

    def load_model(self):
        if not self.save_settings():
            return
        self.emit('ui_action',dict(name='load_model',model=self.model_choice.get()))
        self.headline.set('正在加载 '+self.model_choice.get()+'…')
        self.status['model'].set('加载中…')
        self.pipeline.set('model', 'active', '正在加载 '+self.model_choice.get())
        def load_model():
            self.policy.close()
            self.policy = PolicyProcess(self.config)
            self.controller.policy = self.policy
            self.trial.policy = self.policy
            self.autonomous.policy = self.policy
            self.emit('model_ready', self.policy.start())
        self.work(load_model)

    def _arm_match(self, dry):
        if self.trial.running():raise RuntimeError('请先停止 FL 实战试验')
        if self.busy:raise RuntimeError('请等待连接或模型加载完成')
        self.device.synchronize_clock()
        self.pipeline.reset_execution()
        self.autonomous.start(dry=dry)
        self.battle_view.reset()
        self.debug_arena.reset()
        self.emit('ui_action',dict(name='start_match',model=self.model_choice.get(),dry=dry))
        self.headline.set('已待命：请在模拟器进入经典 1v1')
        self.decision_text.set('等待本局第一次模型动作…')
        self.application_text.set('客户端实际下牌：尚无')

    def start(self):
        try:
            if self.launch_mode is not None or self.series.active or self.autonomous.running():
                raise RuntimeError('已有对战在运行，请先停止或等待本局结束')
            self._arm_match(self.dry.get())
            self.launch_mode='single'
            self.series_status.set('单次对战已待命 · 请手动进入经典 1v1')
            self.refresh_battle_controls()
        except Exception as error:self.emit('error',str(error))

    def model_demo(self):
        if self.autonomous.running():
            self.emit('error','对战中不能重置模型');return
        if self.trial.running():
            self.emit('error', '实战试验运行时不能重置模型')
            return
        if self.controller.thread and self.controller.thread.is_alive():
            self.emit('error', '请停止托管后运行独立模型演示，避免重置对局记忆')
            return
        if not self.policy.process or self.policy.process.poll() is not None:
            self.emit('error', '请先加载 FL 模型')
            return
        self.pipeline.set('model', 'active', '演示数据 → FL 推理；不执行')
        self.work(lambda: self.emit('model_demo', self.policy.request({'op': 'demo'})))

    def check_model_input(self):
        if not self.policy.process or self.policy.process.poll() is not None:
            self.emit('error', '请先加载模型，以检查真实卡牌词表')
            return
        self.pipeline.set('input', 'active', '检查实时字段与 FL 卡牌词表')
        def check():
            snapshot = self.device.stream.latest()
            self.emit('input_report', self.policy.request({'op':'check_live_input', 'snapshot':snapshot})['report'])
        self.work(check)

    def native_test(self):
        if self.autonomous.running():
            self.emit('error','对战中不能混入人工测试动作');return
        if self.trial.running():
            self.emit('error', '实战试验运行时不能混入测试下牌')
            return
        self.pipeline.reset_execution()
        def run():
            snapshot = self.device.stream.latest()
            if not snapshot:
                raise RuntimeError('请先连接设备并进入对局')
            cards = [c for c in snapshot['hand'] if c['cost'] > 0 and c['cost']*10000 <= snapshot['mana_raw'] and c['form'] == 0]
            if not cards:
                raise RuntimeError('当前没有可用于测试的普通形态手牌')
            card = min(cards, key=lambda c: c['cost'])
            action = {'kind': 'play_card', 'owner': snapshot['owner'], 'hand_slot': card['hand_slot'],
                      'card_id': card['card_id'], 'target_grid': [8, 26 if snapshot['owner'] == 1 else 5],
                      'execute_offset_ticks': 1, 'metadata': {'policy_effective_form_code': card['form']}}
            self.device.enable_native(True)
            try:
                self.emit('native_request', self.device.queue_action(action))
            except Exception:
                self.device.cancel_native()
                raise
        self.work(run)

    def stop(self):
        was_series_active = self.series.active
        self.series.stop()
        self.launch_mode = None
        if not was_series_active:
            self.series_status.set('已停止')
        self.debug_arena.mark_stale()
        self.emit('ui_action',dict(name='stop'))
        self.autonomous.stop()
        self.trial.stop()
        self.controller.stop()
        def cancel():
            try:
                self.device.cancel_native()
            except Exception as error:
                self.emit('error', str(error))
        threading.Thread(target=cancel, daemon=True).start()
        self.status['control'].set('停止请求已发出')
        self.headline.set('已停止自动对战')
        self.pipeline.set('submit', 'idle', '停止排队及执行')
        self.refresh_battle_controls()

    def model_trial(self):
        try:
            if self.busy or self.autonomous.running() or self.controller.thread and self.controller.thread.is_alive():
                raise RuntimeError('请等待当前操作完成或停止托管')
            self.pipeline.reset_execution()
            self.trial.start()
        except Exception as error:self.emit('error', str(error))

    def refresh(self):
        self.work(lambda: self.emit('screenshot', self.device.screenshot()))

    def calibrate(self):
        if self.controller.thread and self.controller.thread.is_alive():
            self.emit('error', '请先停止托管再校准')
            return
        if self.image is None:
            self.emit('error', '请先刷新对战截图')
            return
        self.config['calibrated'] = False
        self.calibration_step = 0
        self.hint.set('点击竞技场左上边界，再点击右下边界。')

    def calibration_click(self, event):
        if self.calibration_step is None:
            return
        point = (event.x/self.preview_width, event.y/self.preview_height)
        step = self.calibration_step
        if step == 0:
            self.corner = point
        elif step == 1:
            if point[0] <= self.corner[0] or point[1] <= self.corner[1]:
                self.hint.set('右下边界必须位于左上边界右下方，请重试。')
                return
            self.config['arena'] = [*self.corner, *point]
        else:
            self.config['hand'][step-2] = list(point)
        self.calibration_step += 1
        if self.calibration_step == 6:
            self.calibration_step = None
            self.config['calibrated'] = True
            save(self.config)
            self.hint.set('校准已保存：竞技场及四个手牌槽位。')
        elif self.calibration_step >= 2:
            self.hint.set(f'点击手牌槽位 {self.calibration_step-1} 的中心（从左到右，仅四张可下的牌）。')
        self.draw()

    def draw(self):
        if self.canvas is None:
            return
        from PIL import ImageTk
        w, h = self.preview_width, self.preview_height
        self.canvas.delete('all')
        if self.image:
            self.photo = ImageTk.PhotoImage(self.image.resize((w, h)))
            self.canvas.create_image(0, 0, image=self.photo, anchor='nw')
        left, top, right, bottom = self.config['arena']
        for col in range(19):
            x = (left+(right-left)*col/18)*w
            self.canvas.create_line(x, top*h, x, bottom*h, fill='#58a4d9', stipple='gray50')
        for row in range(33):
            y = (top+(bottom-top)*row/32)*h
            self.canvas.create_line(left*w, y, right*w, y, fill='#58a4d9', stipple='gray50')
        for index, (x, y) in enumerate(self.config['hand']):
            self.canvas.create_oval(x*w-12, y*h-12, x*w+12, y*h+12, outline='#ffd66b', width=2)
            self.canvas.create_text(x*w, y*h, text=str(index+1), fill='#ffd66b')

    def draw_native_field(self, frame):
        if self.canvas is None:
            return
        # Real native positions rendered directly; no repeated screen capture
        # or image inference competes with the policy during a live match.
        w,h=self.preview_width,self.preview_height;arena_h=h-42
        self.canvas.delete('all')
        self.canvas.create_rectangle(0,0,w,h,fill='#eef3f8',outline='')
        self.canvas.create_rectangle(0,0,w,arena_h,fill='#e1eee5',outline='')
        def point(x,y):
            if frame['owner']==0:x,y=18000-x,32000-y
            return x*w/18000,y*arena_h/32000
        for col in range(19):self.canvas.create_line(col*w/18,0,col*w/18,arena_h,fill='#cad9cd')
        for row in range(33):self.canvas.create_line(0,row*arena_h/32,w,row*arena_h/32,fill='#cad9cd')
        self.canvas.create_rectangle(0,15*arena_h/32,w,17*arena_h/32,fill='#95c4d8',outline='')
        for obj in frame['battlefield']['objects']:
            x,y=point(obj['x'],obj['y']);color='#3579bd' if obj['owner']==frame['owner'] else '#c55461'
            tower=obj.get('data_id_40') in (35000000,35000001)
            radius=13 if tower else 5
            self.canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=color,outline='white')
            if tower:self.canvas.create_text(x,y,text=str(obj.get('hp','?')),fill='white',font=('Helvetica',8))
        self.canvas.create_text(w/2,9,text=f"原生实时战场 · tick {frame['tick']}",fill='#243c59',font=('Helvetica',9))
        for card in frame['hand']:
            slot=card['hand_slot'];x=(slot+.5)*w/4
            self.canvas.create_text(x,arena_h+10,text=CARD_NAMES.get(card['card_id'],str(card['card_id'])),font=('Helvetica',8),fill='#243c59')
            self.canvas.create_text(x,arena_h+27,text=f"{card['cost']} 费 · 槽 {slot+1}",font=('Helvetica',8),fill='#536778')

    def add_log(self, text, *, severity='info', category='battle', at=None):
        stamp = datetime.fromisoformat(at).astimezone().strftime('%H:%M:%S') if at else time.strftime('%H:%M:%S')
        row = dict(time=stamp,text=str(text)[:500],severity=severity,category=category)
        self.log_rows.append(row)
        if self.log_matches(row):
            self.log.configure(state='normal')
            self.log.insert('end', f"{stamp}  {row['text']}\n",severity)
            if int(self.log.index('end-1c').split('.')[0]) > 700:
                self.log.delete('1.0', '100.0')
            self.log.see('end')
            self.log.configure(state='disabled')

    def log_matches(self,row):
        selected=self.log_filter.get()
        return (selected=='全部' or selected=='错误' and row['severity']=='error'
            or selected=='告警' and row['severity']=='warning'
            or selected=='设备' and row['category'] in ('device','input')
            or selected=='模型' and row['category']=='model'
            or selected=='下牌' and row['category']=='deploy'
            or selected=='对局' and row['category'] in ('battle','result'))

    def render_logs(self):
        self.log.configure(state='normal')
        self.log.delete('1.0','end')
        for row in self.log_rows:
            if self.log_matches(row):
                self.log.insert('end',f"{row['time']}  {row['text']}\n",row['severity'])
        self.log.see('end')
        self.log.configure(state='disabled')

    def refresh_metrics(self,pending=None):
        tick='—' if self.last_capture_tick is None else str(self.last_capture_tick)
        lag='—' if self.last_capture_lag is None else f'{self.last_capture_lag:.0f} ms'
        inference='—' if self.last_inference_ms is None else f'{self.last_inference_ms:.0f} ms'
        waiting = len(self.autonomous.pending) if pending is None else pending
        self.live_metrics.set(f'采集 tick {tick}  · 传输 {lag}  · 推理 {inference}  · 待应用 {waiting}')

    def export_diagnostics(self):
        status={name:variable.get() for name,variable in self.status.items()}
        status.update(headline=self.headline.get(),last_capture_tick=self.last_capture_tick,
            last_capture_lag_ms=self.last_capture_lag,last_inference_ms=self.last_inference_ms,
            autonomous_running=self.autonomous.running(),device_connected=self.device.connected,
            model_running=bool(self.policy.process and self.policy.process.poll() is None),
            pending_requests=len(self.autonomous.pending))
        config=dict(self.config)
        recent=list(self.journal.recent)
        self.work(lambda:self.emit('diagnostics_exported',str(export_report(ROOT,status=status,
            config=config,recent=recent))))

    def drain(self):
        pending = False
        try:
            pending = self._drain_events()
        except Exception as error:
            detail=dict(message=str(error),operation='gui_event_loop',traceback=traceback.format_exc())
            self.last_error.set('最近错误：'+str(error))
            self.headline.set('需要处理：'+str(error))
            logged=self.journal.record('exception',detail)
            if logged:
                self.add_log(logged['message'],severity='error',category='error',at=logged['at'])
        finally:
            # A large stream of live frames must yield to click/paint events.
            # Catch up in short slices rather than blocking one Tk callback.
            self.root.after(10 if pending else 100,self.drain)

    def _drain_events(self):
        deadline = time.monotonic() + .008
        for _ in range(200):
            if time.monotonic() >= deadline:
                break
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == 'screenshot':
                self.image = value
                self.draw()
                continue
            logged=self.journal.record(event,value)
            if logged:
                self.add_log(logged['message'],severity=logged['severity'],category=logged['category'],at=logged['at'])
            if event == 'connected':
                self.status['device'].set(f"{self.config['serial']} · PID {value} · {self.config['expected_version']}")
                self.add_log(f"设备时钟校准：主机比模拟器快 {self.device.stream.clock_offset_ms:.0f} ms")
                self.headline.set('设备已连接；请加载模型' if not self.policy.process else '准备完成，可以开始下一局')
                self.pipeline.set('device', 'done', '设备及 overlay 已连接')
            elif event == 'model_ready':
                self.status['model'].set(self.model_choice.get() + ' · CPU')
                self.headline.set('准备完成，可以开始下一局' if self.device.connected else '模型已加载；请连接设备')
                self.pipeline.set('model', 'done', self.model_choice.get()+' 已加载 · CPU')
            elif event == 'input_report':
                self.show_input_report(value)
            elif event == 'model_demo':
                decisions = value['decisions']
                latency = sum(d['inference_ms'] for d in decisions)/len(decisions)
                self.pipeline.set('model', 'done', f'离线推理 {len(decisions)} 次 · {latency:.1f} ms')
                self.decision_text.set('离线演示 / 合成输入 / 不执行\n' + '\n'.join(self.describe_decision(d) for d in decisions))
                continue
            elif event == 'native_request':
                req = value['request']
                if req.get('kind')=='activate_ability':
                    self.pipeline.set('submit','active',f"技能 {req['ability_id']} · 实体 {req['source_entity']}")
                    self.pipeline.set('receipt','active','等待原生技能应用')
                    self.application_text.set(f"客户端技能：{req['ability_id']} 已提交，等待应用")
                else:
                    self.pipeline.set('submit', 'active', f"slot {req['hand_slot']+1} → ({req['x']},{req['y']})")
                    self.pipeline.set('receipt', 'active', '等待原生部署应用')
                    self.application_text.set(f"客户端实际下牌：卡牌 {CARD_NAMES.get(req['card_id'],req['card_id'])} 已提交，等待应用")
                self.battle_view.submitted(req)
            elif event == 'control_status':
                self.status['control'].set(value)
            elif event == 'trial_mapping':
                mapping = value['mapping']
                self.pipeline.set('input', 'done', f"实测 {mapping['towers']} 塔 / {mapping['entities']} 单位")
                self.input_hint.set(f"实时战场：{mapping['towers']} 座塔、{mapping['entities']} 个单位；模型持续接收新观测")
                if value.get('tick')==0 and self.autonomous.directory is not None:
                    self.series.on_battle_started(self.autonomous.directory)
                continue
            elif event in ('trial_decision','battle_decision'):
                self.pipeline.set('model', 'done', f"实时 FL · tick {value['tick']} · {value['inference_ms']:.1f} ms")
                self.last_inference_ms=value['inference_ms']
                self.refresh_metrics()
                if event == 'trial_decision' or any(a['kind'] not in ('wait','wait_until_affordable') for a in value['actions']):
                    self.decision_text.set(self.describe_decision(value))
                self.battle_view.decision(value)
                if value.get('observation'):
                    self.battle_view.observation(value['observation'],value.get('inference_ms'))
                    self.debug_arena.observation(value['observation'])
            elif event == 'battle_progress':
                mode_label='连续对战' if self.launch_mode=='continuous' else '单次对战'
                self.status['control'].set(f"{mode_label} · 决策 {value['decisions']} / 下牌 {value['applied']} / 等待 {value['pending']}")
                self.headline.set(f"模型正在对局 · 已下牌 {value['applied']} 次")
                self.refresh_metrics(value['pending'])
                continue
            elif event == 'battle_application':
                self.pipeline.set('receipt','done',f"实际应用 {value['applied']} 次 · tick {value['receipt']['tick']}")
                receipt=value['receipt']
                if receipt.get('kind')=='activate_ability':
                    self.application_text.set(f"客户端技能已生效：{receipt['ability_id']} · tick {receipt['tick']}")
                else:
                    self.application_text.set(f"客户端实际下牌：{CARD_NAMES.get(receipt['card_id'],receipt['card_id'])} · tick {receipt['tick']} · 本局第 {value['applied']} 次")
                self.battle_view.applied(receipt,value['applied'])
            elif event == 'battle_fizzle':
                receipt=value['receipt']
                self.application_text.set(f"技能未生效：{receipt['ability_id']} · 英雄阵亡 · tick {receipt['tick']}")
                self.battle_view.fizzled(receipt)
            elif event == 'expired_action':
                self.application_text.set(f"模型动作未提交：{value['reason']} · 决策 tick {value['decision_tick']}")
                self.battle_view.skipped(value)
            elif event == 'card_gap_blocked':
                self.application_text.set(f"下牌间隔阻止提交：还剩 {value['remaining_ms']} ms · 决策 tick {value['decision_tick']}")
            elif event == 'decision_pacing' and value['interval_ms']>250:
                self.pipeline.set('model','active',f"推理 {value['inference_ms']:.0f} ms · 降频至 {value['interval_ms']} ms")
            elif event == 'battle_finished':
                self.debug_arena.mark_stale()
                self.headline.set('对局结束，正在确认结果…')
                if self.launch_mode == 'single':
                    self.series_status.set('单次对战已结束 · 正在确认结果')
                self.series.on_battle_finished(value['directory'])
                self.root.after(100,self.schedule_result)
            elif event == 'match_file':
                self.battle_view.record_file.set('本局 JSONL：'+str(value))
            elif event == 'match_result':
                self.headline.set('上局对局已结束，可以开始下一局')
                if self.launch_mode == 'single':
                    self.launch_mode = None
                    self.series_status.set('单次对战已完成 · 结果：'+value['result'])
                self.series.on_match_result(value['directory'],value['result'])
            elif event == 'continuous_tap_result':
                self.series.on_tap_result(value['kind'],value.get('error'))
            elif event == 'trial_evidence':
                self.add_log('本次模型试验记录已保存至 records；应用事件另见 app-events.jsonl')
                continue
            elif event == 'trial_success':
                (ROOT / 'records/last-trial.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
                self.pipeline.set('submit', 'done', 'FL 自主选择的动作已执行')
                self.pipeline.set('receipt', 'done', f"tick {value['receipt']['tick']} · 战场实际应用")
                self.status['control'].set('FL 模型下牌成功；单次试验完成')
                self.work(self.capture_trial_result)
            elif event == 'probe':
                kind = value.get('event')
                if kind == 'opponent_elixir_probe_ready':
                    self.status['probe'].set('已挂载；等待对局')
                elif kind == 'native_partial_frame':
                    frame = value['frame']
                    for deployment in self.battle_view.public_deployments(frame):
                        public_log=self.journal.record('public_deployment',deployment)
                        if public_log:
                            self.add_log(public_log['message'],severity=public_log['severity'],
                                category=public_log['category'],at=public_log['at'])
                    self.last_stream_at = time.monotonic()
                    self.stream_stale = False
                    self.last_capture_tick=frame['tick']
                    self.last_capture_lag=self.device.stream.last_latency_ms
                    self.refresh_metrics()
                    if self.autonomous.running() and frame.get('battlefield'):self.draw_native_field(frame)
                    self.status['battle'].set(f"tick {frame['tick']} · 我方圣水 {frame['mana_raw']/10000:.2f}")
                    self.pipeline.set('device', 'done', f"tick {frame['tick']} · 圣水 {frame['mana_raw']/10000:.2f}")
                    self.status['probe'].set('实时状态流已接入')
                    if (not self.busy and self.autonomous.directory is None and not self.autonomous.running() and self.policy.process and self.policy.process.poll() is None
                            and self.auto_checked_world != (frame['world'], frame['generation'])):
                        self.auto_checked_world = (frame['world'], frame['generation'])
                        self.check_model_input()
                    lag = self.device.stream.last_latency_ms
                    if frame.get('battlefield'):
                        self.status['coverage'].set(f"基础战场观测已接入 · 传输 {lag:.1f} ms；特殊运行语义待适配")
                    else:
                        self.status['coverage'].set(f"局部观测 · 传输 {lag:.1f} ms；{frame.get('mapping_error', '等待战场数据')}")
                    continue
                elif kind == 'native_runtime_ready':
                    self.status['native'].set('游戏线程 mailbox 就绪；执行关闭')
                elif kind == 'native_deploy_receipt':
                    stage = value.get('stage')
                    self.status['native'].set(str(stage or value.get('error')))
                    if stage == 'battle_applied':
                        self.pipeline.set('submit', 'done', '原生命令已执行')
                        confirmed='技能次数变化' if value.get('kind')=='activate_ability' else '手牌轮换'
                        self.pipeline.set('receipt', 'done', f"tick {value['tick']} · 已确认{confirmed}")
                    elif value.get('ok') or value.get('deferred'):
                        self.pipeline.set('submit', 'active', '客户端已排队；等待应用')
                    elif stage == 'source_despawned':
                        self.pipeline.set('receipt','done','英雄阵亡，技能未生效；继续对局')
                    else:
                        self.pipeline.set('submit', 'error', str(value.get('error', '提交未通过')))
                elif kind == 'native_deploy_validation':
                    self.pipeline.set('receipt', 'active', '已命中原生部署验证')
                elif kind == 'elixir_battle_changed':
                    self.status['battle'].set(value['battle_state'] + ' · 首次部署触发')
                elif kind == 'elixir_sample':
                    continue
                elif kind == 'deploy_elixir_getter_result':
                    continue
            elif event == 'decision':
                self.decision_count += 1
                self.pipeline.set('model', 'done', f"tick {value.get('tick')} · {value.get('inference_ms', 0):.1f} ms")
                self.last_inference_ms=value.get('inference_ms')
                self.refresh_metrics()
                self.decision_text.set(self.describe_decision(value))
                self.status['coverage'].set(f"已接收完整帧 · tick {value.get('tick')} · 推理 {value.get('inference_ms', 0):.1f} ms")
            elif event == 'diagnostics_exported':
                self.report_path.set('诊断报告：'+str(value))
                self.open_logs()
            if event in ('error','exception'):
                self.debug_arena.mark_stale()
                message=value.get('message',str(value)) if isinstance(value,dict) else str(value)
                self.series.fail(message)
                self.status['control'].set('错误：' + message)
                self.headline.set('需要处理：'+message)
                self.last_error.set('最近错误：'+message)
                if self.launch_mode == 'single' and self.autonomous.last_result and not self.autonomous.running():
                    self.launch_mode = None
                    self.series_status.set('单次对战已停止 · '+message)
        self.refresh_battle_controls()
        if self.last_stream_at is not None and time.monotonic()-self.last_stream_at > 1 and not self.stream_stale:
            self.stream_stale=True
            self.pipeline.set('device', 'blocked', '实时状态流已停止；等待对局')
            self.status['battle'].set('状态流已停止，等待有效对局')
        return not self.events.empty()

    @staticmethod
    def describe_decision(result):
        actions = [a for a in result.get('actions', []) if a['kind'] not in ('wait', 'wait_until_affordable')]
        if not actions:
            return '模型正在等待合适时机'
        return '；'.join(
            f"准备打出 {CARD_NAMES.get(a.get('card_id'), a.get('card_id'))}"
            if a['kind']=='play_card' else
            f"准备释放技能 {a.get('ability_id')} · 实体 {a.get('source_entity')}"
            if a['kind']=='activate_ability' else f"模型动作：{a['kind']}" for a in actions)

    def show_input_report(self, report):
        missing = '、'.join(report['missing_labels'])
        self.input_hint.set(f"实时腾讯状态 · tick {report['tick']} · 传输后年龄 {report['age_ms']:.1f} ms")
        self.status['coverage'].set('输入完整' if report['ready'] else '待映射：' + missing)
        self.pipeline.set('input', 'done' if report['ready'] else 'blocked',
                          '完整输入就绪' if report['ready'] else '已接入手牌；战场字段待映射')

    def close(self):
        self.series.stop()
        if self.floating_targets is not None:self.floating_targets.destroy()
        self.autonomous.stop()
        self.trial.stop()
        self.controller.stop()
        self.policy.close()
        self.device.close()
        self.journal.close()
        self.root.destroy()

    def capture_trial_result(self):
        image = self.device.screenshot()
        image.save(ROOT / 'records/last-trial.png')
        self.emit('screenshot', image)

    def schedule_result(self):
        if self.busy:self.root.after(250,self.schedule_result)
        else:self.work(self.detect_result)

    def record_confirmed_result(self, summary, result, evidence, source):
        stats=self.ledger.record(summary,result,str(evidence),source=source)
        summary.update(result=result,result_source=source,result_evidence=str(evidence))
        (Path(summary['directory'])/'summary.json').write_text(
            json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        self.emit('match_result',dict(result=result,stats=stats,
            directory=summary['directory'],source=source))

    def detect_result(self):
        from pathlib import Path
        from .native_result import terminal_result, save_terminal_evidence
        # The final native frame can reach the event log after the battle-ended
        # notification. Wait for a consistent local tower-state proof.
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            summary=self.autonomous.last_result
            if not summary:
                time.sleep(.2);continue
            proof=terminal_result(summary['directory'])
            if proof is not None:
                evidence=save_terminal_evidence(summary['directory'],proof)
                self.record_confirmed_result(summary,proof['result'],evidence,'native_terminal')
                return
            time.sleep(.5)
        self.emit('error','本地终局塔状态不足以判断胜负，本局保留未确认')

def main():
    sys.path.insert(0, load()['firstlight'])
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == '__main__':
    main()
