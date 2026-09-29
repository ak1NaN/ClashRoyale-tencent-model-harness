"""Compact, event-driven presentation of the actual integration stages."""
import tkinter as tk

class PipelinePanel(tk.Frame):
    COLORS = {'idle':'#71849e','active':'#399de1','done':'#38a589','blocked':'#d59b35','error':'#d76060'}
    def __init__(self, parent):
        super().__init__(parent, bg='#eef3f8')
        self.items={}
        for index,(key,title,detail) in enumerate([
            ('device','01 读取对局','连接设备与 overlay'),
            ('input','02 转换输入','检查 FL 所需字段'),
            ('model','03 FL 模型','加载权重与循环记忆'),
            ('submit','04 原生下牌','排队并调用游戏引擎'),
            ('receipt','05 应用回执','核对部署与手牌变化')]):
            box=tk.Frame(self,bg='white',highlightbackground='#d9e2ee',highlightthickness=1,padx=11,pady=10)
            box.grid(row=0,column=index,sticky='nsew',padx=(0,8 if index<4 else 0))
            self.columnconfigure(index,weight=1,uniform='step')
            title_label=tk.Label(box,text=title,bg='white',fg='#213956',font=('Helvetica',12,'bold'),anchor='w')
            title_label.pack(fill='x')
            message=tk.Label(box,text=detail,bg='white',fg='#71849e',font=('Helvetica',10),anchor='w',justify='left',wraplength=210,height=2)
            message.pack(fill='x',pady=(5,0))
            self.items[key]=(box,title_label,message)

    def set(self,key,state,detail):
        box,title,label=self.items[key]
        color=self.COLORS[state]
        box.configure(highlightbackground=color)
        label.configure(text=detail,fg=color)

    def reset_execution(self):
        self.set('submit','idle','等待模型动作或单次测试')
        self.set('receipt','idle','尚无本次部署回执')
