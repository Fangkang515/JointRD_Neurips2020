import pdb, torch, math
from scipy.stats import norm
import torch.nn.functional as F
import torch.nn        as nn
from torch.utils.checkpoint import checkpoint

def distillation_loss(source, target, margin):
    loss = ((source - margin)**2 * ((source > margin) & (target <= margin)).float() +
            (source - target)**2 * ((source > target) & (target > margin) & (target <= 0)).float() +
            (source - target)**2 * (target > 0).float())
    return torch.abs(loss).sum()

def build_feature_connector(s_channel, t_channel):
    C = [nn.Conv2d(s_channel, t_channel, kernel_size=1, stride=1, padding=0, bias=False),
         nn.BatchNorm2d(t_channel)]

    for m in C:
        if isinstance(m, nn.Conv2d):
            n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            m.weight.data.normal_(0, math.sqrt(2. / n))
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1)
            m.bias.data.zero_()
    return nn.Sequential(*C)

def get_margin_from_BN(bn):
    margin = []
    std = bn.weight.data
    mean = bn.bias.data
    for (s, m) in zip(std, mean):
        s = abs(s.item())
        m = m.item()
        if norm.cdf(-m / s) > 0.001:
            margin.append(- s * math.exp(- (m / s) ** 2 / 2) / math.sqrt(2 * math.pi) / norm.cdf(-m / s) + m)
        else:
            margin.append(-3 * s)
    return torch.FloatTensor(margin).to(std.device)

class Distiller(nn.Module):
    
    def __init__(self, student, teacher, config_t, config_s, kd_type, num_classes, logger):
        super(Distiller, self).__init__()
        
        self.student = student(config_s)
        self.teacher = teacher(config_t)

        self.slayer_blocks = self.student.get_layer_blocks()
        self.num_stages = len(self.slayer_blocks)
        self.basec = self.student.get_base_channel()
        self.se_index = None
        self.kd_type = kd_type 
        self.window = True
        self.student_blockchoice = self.student.get_blockchoice()
        self.teacher_blockchoice = self.teacher.get_blockchoice()
        self.dis_point = sorted(set([2, 6, 12]))
        
        if kd_type == "margin":
            self.Connectors = nn.ModuleList([build_feature_connector(t, s) for t, s in zip([512]*4,  [512]*4)])

        logger.log("dis_point-->{}".format(self.dis_point))
        logger.log("student: block_choice-->{}".format(self.student_blockchoice))
        logger.log("teacher: block_choice-->{}".format(self.teacher_blockchoice))


    def forward(self, x, **kwargs):
        
        stage = kwargs.get("stage")
        
        if stage == "TA1" or stage == "JOINT":
            return self.forward_ta1(x, **kwargs)  
        elif "RES_NMT" in stage:
            return self.teacher(x, **kwargs)
        elif "CNN_NMT" in stage:
            return self.student(x, **kwargs)
        elif "RES_KD" in stage:
            return self.forward_tkd(x, **kwargs)
        else:
            raise NameError("invalid stage name")
            
    def forward_tkd(self, x, **kwargs):
        with torch.no_grad():
            teacher_logits = self.teacher(x, **kwargs)
        student_logits = self.student(x, **kwargs)
        return student_logits, teacher_logits    
    
    
    def forward_ta1(self, x, **kwargs):  
        fea_final_student = []
        out_final_student = []
        ##########run student####################
        student_logits, student_feas = self.student.forward_to(x, dis_point=self.dis_point) 
        pdb.set_trace()
        assert len(student_feas) == len(self.dis_point)
        for pos in range(len(self.dis_point)):
            student_fea = student_feas[pos]
            print(student_fea.shape)
            student_out, student_fea = self.teacher.forward_from(student_fea, se_index=self.dis_point[pos]+1)
            fea_final_student.append(student_fea[-1])
            out_final_student.append(student_out)

        fea_final_student.append(student_feas[-1])
        out_final_student.append(student_logits)
        ##########run teacher####################
        if kwargs.get("stage") == "JOINT":
            self.teacher.train()
            teacher_logits, teacher_feas = self.teacher.forward_to(x,  dis_point=self.dis_point)
            teacher_feas = teacher_feas[-1]
            self.teacher.eval()
        else:
            with torch.no_grad():
                teacher_logits, teacher_feas = self.teacher.forward_to(x)
                teacher_feas = teacher_feas[-1]
        ##########kd#############################  
        loss_distill = []  
        if self.kd_type == "margin":
            for i, s_fea in enumerate(fea_final_student):
                self.reset_margin()
                s_fea = self.Connectors[i](s_fea)
                loss_distill.append(distillation_loss(s_fea, teacher_feas.detach(), getattr(self, 'margin%d' % (1))) / (teacher_feas.detach().shape[0]))
                               
        elif self.kd_type == "nst":
            loss_distill = 0
            for s_fea in s_student:
                loss_distill += self.nst(s_fea, t_fea.detach())
            loss_distill = loss_distill / len(s_student)
            
        elif self.kd_type == "none":
                loss_distill = None
        else:
            raise NameError("not implement")

        return out_final_student, teacher_logits, loss_distill
    

        
    def reset_margin(self):        
        with torch.no_grad():
            layers = sum(sum(l) for l in self.teacher_blockchoice)
            teacher_bns = self.teacher.get_bn_before_relu([layers - 1])   
            margins     = [get_margin_from_BN(bn) for bn in teacher_bns]
            for i, margin in enumerate(margins):
                self.register_buffer('margin%d' % (i+1), margin.unsqueeze(1).unsqueeze(2).unsqueeze(0).detach())
                
    def get_thismodel(self, epoch, batch_pro):
        se_pos = epoch // batch_pro
        if se_pos <= len(self.dis_point) - 1:
            stride = self.dis_point[se_pos] - self.dis_point[se_pos-1] if se_pos > 0 else (self.dis_point[se_pos]+1)
            start = self.dis_point[se_pos] - stride + 1
            end   = self.dis_point[se_pos]
            paramname = self.get_params(start, end)
        else:
            paramname = None
        return paramname
    
    def get_params(self, start, end):
        layer_index = 0
        paramnames = []
        last_layernum = 0
        for k, v in self.named_parameters():
            if "student" in k:
                if start == 0:
                    if "fc" not in k and "layers" not in k:
                        paramnames.append("module." + k)
            if "layers" in k:
                layernum = int(k.split(".")[3])
                
                if layernum != last_layernum:
                    layer_index += 1
                if layer_index >= start and layer_index <=end:
                    paramnames.append("module." + k)
                last_layernum = layernum
                    
        return paramnames
       
    def get_se_index(self):
        return print(self.se_index)
    
    def get_respos(self):
        layer_index = 0
        pos = []
        choices = self.student.get_blockchoice()
        for i in range(len(choices)):
            for j in range(len(choices[i])):
                if choices[i][j]:
                        pos.append((i, j))
        return pos
                
            
    
                
                
        
        
    
    