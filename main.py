import sys
import argparse
import os
import time
import shutil
import torch
import torchvision
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
from torch.nn.utils import clip_grad_norm_

from ops.dataset import TSNDataSet
from ops.models import TSN
from ops.transforms import *
from opts import parser


best_prec1 = 0


def main():
    global args, best_prec1
    args = parser.parse_args()
    
    print("------------------------------------")
    print("Environment Versions:")
    print("- Python: {}".format(sys.version))
    print("- PyTorch: {}".format(torch.__version__))
    print("- TorchVison: {}".format(torchvision.__version__))
    
    args_dict = args.__dict__
    print("------------------------------------")
    print(args.arch+" Configurations:")
    for key in args_dict.keys():
        print("- {}: {}".format(key, args_dict[key]))
    print("------------------------------------")
    print (args.mode)
    if args.dataset == 'ucf101':
        num_class = 101
        rgb_read_format = "{:05d}.jpg"
    elif args.dataset == 'hmdb51':
        num_class = 51
        rgb_read_format = "{:05d}.jpg"        
    elif args.dataset == 'kinetics':
        num_class = 400
        rgb_read_format = "{:05d}.jpg"
    elif args.dataset == 'something':
        num_class = 174
        rgb_read_format = "{:05d}.jpg"
    elif args.dataset == 'NTU_RGBD':
        num_class = 120
        rgb_read_format = "{:05d}.jpg"                
    elif args.dataset == 'tinykinetics':
        num_class = 150
        rgb_read_format = "{:05d}.jpg"        
    else:
        raise ValueError('Unknown dataset '+args.dataset)

    model = TSN(num_class, args.num_segments, args.pretrained_parts, args.modality,
                base_model=args.arch,
                consensus_type=args.consensus_type, dropout=args.dropout, partial_bn=not args.no_partialbn)

    crop_size = model.crop_size
    scale_size = model.scale_size
    input_mean = model.input_mean
    input_std = model.input_std
    
    policies = model.get_optim_policies(args.dataset)

    train_augmentation = model.get_augmentation()

    model = torch.nn.DataParallel(model, device_ids=args.gpus).cuda()

    model_dict = model.state_dict()

    print("pretrained_parts: ", args.pretrained_parts)

    if args.arch == "resnet50":
        new_state_dict = {} 
        div = False
        roll = True 
    elif args.arch == "resnet34":
        pretrained_dict={}
        new_state_dict = {} 
        for k, v in model_dict.items():
            if ('fc' not in k):
                new_state_dict.update({k:v})
        div = False
        roll = True  
    elif args.arch == "Res3D18":
        pretrained_dict={}
        new_state_dict = {} 
        for k, v in model_dict.items():
            if ('fc' not in k):
                new_state_dict.update({k:v})
        div = False
        roll = True 
    elif args.arch == "TSM":
        pretrained_dict={}
        new_state_dict = {}
        for k, v in model_dict.items():
            if ('fc' not in k):
                new_state_dict.update({k:v})
        div = True
        roll = False             
    elif (args.arch == "SELFY" ):
        pretrained_dict={}
        new_state_dict = {}
        for k, v in model_dict.items():
            if ('fc' not in k):
                new_state_dict.update({k:v})
        div = True
        roll = False                    
    
    print("------------------------------------")
    print("Model:\n{}".format(model))
    print("------------------------------------")

    if args.resume:
        if os.path.isfile(args.resume):
            print(("=> loading checkpoint '{}'".format(args.resume)))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            print(("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch'])))
        else:
            print(("=> no checkpoint found at '{}'".format(args.resume)))

    cudnn.benchmark = True

    # Data loading code
    if args.modality != 'RGBDiff':
        normalize = GroupNormalize(input_mean, input_std)
    else:
        normalize = IdentityTransform()

    if args.modality == 'RGB':
        data_length = 1
    elif args.modality in ['Flow', 'RGBDiff']:
        data_length = 1

    train_loader = torch.utils.data.DataLoader(
        TSNDataSet("", args.train_list, num_segments=args.num_segments,
                   new_length=data_length,
                   modality=args.modality,
                   mode = args.mode,
                   image_tmpl=args.rgb_prefix+rgb_read_format if args.modality in ["RGB", "RGBDiff"] else args.flow_prefix+rgb_read_format,
                   transform=torchvision.transforms.Compose([
                       GroupScale((240,320)),                              
                       train_augmentation,
                       Stack(roll=roll),
                       ToTorchFormatTensor(div=div),
                       normalize,
                   ])),
        batch_size=args.batch_size, shuffle=True,  
        num_workers=args.workers, pin_memory=True)

    val_loader = torch.utils.data.DataLoader(
        TSNDataSet("", args.val_list, num_segments=args.num_segments,
                   new_length=data_length,
                   modality=args.modality,
                   mode =args.mode,
                   image_tmpl=args.rgb_prefix+rgb_read_format if args.modality in ["RGB", "RGBDiff"] else args.flow_prefix+rgb_read_format,
                   random_shift=False,
                   transform=torchvision.transforms.Compose([
                       GroupScale((240,320)),      
                       GroupCenterCrop(crop_size),
                       Stack(roll=roll),
                       ToTorchFormatTensor(div=div),
                       normalize,
                   ])),
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    if args.loss_type == 'nll':
        criterion = torch.nn.CrossEntropyLoss().cuda()
    else:
        raise ValueError("Unknown loss type")

    for group in policies:
        print(('group: {} has {} params, lr_mult: {}, decay_mult: {}'.format(
            group['name'], len(group['params']), group['lr_mult'], group['decay_mult'])))

    optimizer = torch.optim.SGD(policies,
                                args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay,nesterov=args.nesterov)
    
    output_list = []
    
    if args.evaluate:
        prec1, score_tensor = validate(val_loader,model,criterion,0)
        output_list.append(score_tensor)
        save_validation_score(output_list, filename='score.pt')
        print("validation score saved in {}".format('/'.join((args.val_output_folder, 'score_inf5.pt'))))    
        return

    for epoch in range(args.start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch, args.lr_steps)
        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch)

        # evaluate on validation set
        if (epoch + 1) % args.eval_freq == 0 or epoch == args.epochs - 1:
            prec1, score_tensor = validate(val_loader, model, criterion, (epoch + 1) * len(train_loader))

            output_list.append(score_tensor)            
            
            # remember best prec@1 and save checkpoint
            is_best = prec1 > best_prec1
            best_prec1 = max(prec1, best_prec1)
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
            }, is_best)
            
    # save validation score
    save_validation_score(output_list)
    print("validation score saved in {}".format('/'.join((args.val_output_folder, 'score.pt'))))            


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
   
    # In PyTorch 0.4, "volatile=True" is deprecated.
    torch.set_grad_enabled(True)

    if args.no_partialbn:
        model.module.partialBN(False)
    else:
        model.module.partialBN(True)

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        # discard final batch
        if i == len(train_loader)-1:
            break
        
#         if i == 1:
#             break
        
        # measure data loading time
        data_time.update(time.time() - end)

        # target size: [batch_size]
        input_var = input
        target = target.cuda(async=True)
        target_var = target

      
        output = model(input_var)
        loss = criterion(output, target_var)          
           
        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1,5))
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))
        top5.update(prec5.item(), input.size(0))

        # compute gradient and do SGD step
        loss.backward()
        
        if i % args.iter_size == 0:
            # scale down gradients when iter size is functioning
            if args.iter_size != 1:
                for g in optimizer.param_groups:
                    for p in g['params']:
                        p.grad /= args.iter_size

            if args.clip_gradient is not None:
                total_norm = clip_grad_norm_(model.parameters(), args.clip_gradient)
                if total_norm > args.clip_gradient:
                    print("clipping gradient: {} with coef {}".format(total_norm, args.clip_gradient / total_norm))
            else:
                total_norm = 0
            
            optimizer.step()
            optimizer.zero_grad()


        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print(('Epoch: [{0}][{1}/{2}], lr: {lr:.5f}\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'                   
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   epoch, i, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=losses, top1=top1, top5=top5, lr=optimizer.param_groups[-2]['lr'])))
    
    return

def validate(val_loader, model, criterion, iter, logger=None):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # another losses
    flow_con_losses = AverageMeter()       
    
    torch.set_grad_enabled(False)
    
    # switch to evaluate mode
    model.eval()

    output_list = []
    pred_arr = []
    target_arr = []
    end = time.time()
    for i, (input, target) in enumerate(val_loader):
        # discard final batch
        if i == len(val_loader)-1:
            break
            
        input_var = input
        target = target.cuda(async=True)
        target_var = target
        
        # compute output
        output= model(input_var)
        loss = criterion(output, target_var)          
        
        # class acc
        pred = torch.argmax(output.data, dim=1)
        pred_arr.extend(pred)
        target_arr.extend(target)
        
        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1,5))
        
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))
        top5.update(prec5.item(), input.size(0))    
        
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        output_list.append(output)        
        
        if i % args.print_freq == 0:
            print(('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.4f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'                  
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   i, len(val_loader), batch_time=batch_time, loss=losses, top1=top1, top5=top5)))

    output_tensor = torch.cat(output_list, dim=0)

    print(('Testing Results: Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f} Loss {loss.avg:.5f} Time {batch_time.avg:.4f}'
          .format(top1=top1, top5=top5, loss=losses, batch_time=batch_time)))  
    
    return top1.avg, output_tensor


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    filename = '_'.join((args.snapshot_pref, args.modality.lower(), "epoch", str(state['epoch']), filename))
    torch.save(state, filename)
    if is_best:
        best_name = '_'.join((args.snapshot_pref, args.modality.lower(), 'model_best.pth.tar'))
        shutil.copyfile(filename, best_name)

        
def save_validation_score(score, filename='score.pt'):
    filename = '/'.join((args.val_output_folder, filename))
    torch.save(score, filename)        

    
class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch, lr_steps):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    decay = 0.1 ** (sum(epoch >= np.array(lr_steps)))
    lr = args.lr * decay
    decay = args.weight_decay
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr * param_group['lr_mult']
        param_group['weight_decay'] = decay * param_group['decay_mult']


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))
    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()
