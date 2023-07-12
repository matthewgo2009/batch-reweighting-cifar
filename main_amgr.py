import os
from pprint import pprint
from tqdm import tqdm
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
from torch.utils.tensorboard import SummaryWriter
import utils
from model import resnet32
from config import get_arguments
import numpy as np
import math
import time

parser = get_arguments()
args = parser.parse_args()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
args.device = device
exp_loc, model_loc = utils.log_folders(args)
writer = SummaryWriter(log_dir=exp_loc)


def main():
    """Main script"""

    assert not (args.logit_adj_post and args.logit_adj_train)
    # train_dataset, val_loader, num_train = utils.get_loaders(args)
    train_loader, val_loader, num_train= utils.get_loaders_v2(args)

    num_class = len(args.class_names)
    model = torch.nn.DataParallel(resnet32(num_classes=num_class))
    model = model.to(device)
    cudnn.benchmark = True
    criterion = nn.CrossEntropyLoss(reduction='none').to(device)
    
    ####create z initialization#########
    z = np.zeros(num_train)

    gamma = args.gamma
 
    if args.logit_adj_post:
        if os.path.isfile(os.path.join(model_loc, "model.th")):
            print("=> loading pretrained model ")
            checkpoint = torch.load(os.path.join(model_loc, "model.th"))
            model.load_state_dict(checkpoint['state_dict'])
            for tro in args.tro_post_range:
                args.tro = tro
                args.logit_adjustments = utils.compute_adjustment(train_loader, tro, args)
                val_loss, val_acc = validate(val_loader, model, criterion)
                results = utils.class_accuracy(val_loader, model, args)
                results["OA"] = val_acc
                pprint(results)
                hyper_param = utils.log_hyperparameter(args, tro)
                writer.add_hparams(hparam_dict=hyper_param, metric_dict=results)
                writer.close()
        else:
            print("=> No pre trained model found")

        return

    # args.logit_adjustments = utils.compute_adjustment(train_loader, args.tro_train, args)

    optimizer = torch.optim.SGD(model.parameters(),
                                args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay,
                                nesterov=True)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                        milestones=args.scheduler_steps)

    loop = tqdm(range(0, args.epochs), total=args.epochs, leave=False)
    val_loss, val_acc = 0, 0
    for epoch in loop:
         # train for one epoch
        # train_loss, train_acc = train(train_dataset, model, criterion, optimizer,num_train,gamma,z,epoch)
        train_loss, train_acc = train_v2(train_loader, model, criterion, optimizer, num_train, gamma, z, epoch)
        writer.add_scalar("train/acc", train_acc, epoch)
        writer.add_scalar("train/loss", train_loss, epoch)
        lr_scheduler.step()

        # evaluate on validation set
        if (epoch % args.log_val) == 0 or (epoch == (args.epochs - 1)):
            val_loss, val_acc = validate(val_loader, model, criterion)
            writer.add_scalar("val/acc", val_acc, epoch)
            writer.add_scalar("val/loss", val_loss, epoch)

        loop.set_description(f"Epoch [{epoch}/{args.epochs}")
        loop.set_postfix(train_loss=f"{train_loss:.2f}", val_loss=f"{val_loss:.2f}",
                         train_acc=f"{train_acc:.2f}",
                         val_acc=f"{val_acc:.2f}")

    file_name = 'model.th'
    mdel_data = {"state_dict": model.state_dict()}
    torch.save(mdel_data, os.path.join(model_loc, file_name))

    results = utils.class_accuracy(val_loader, model, args)
    results["OA"] = val_acc
    hyper_param = utils.log_hyperparameter(args, args.tro_train)
    pprint(results)
    writer.add_hparams(hparam_dict=hyper_param, metric_dict=results)
    writer.close()


def compute_grad(sample, target, criterion, model):
    # start_time = time.time()

    
    sample = sample.unsqueeze(0)  # prepend batch dimension for processing
    target = target.unsqueeze(0)

    prediction = model(sample)
    loss = criterion(prediction, target)

    grad = torch.autograd.grad(loss,  list(model.parameters())[-1] )
    # print("---compute_grad runtime is %s seconds ---" % (time.time() - start_time))
 
    return grad



def compute_loss(params, partial_para, buffers, sample, target,criterion):
    batch = sample.unsqueeze(0)
    targets = target.unsqueeze(0)

    predictions = functional_call(model, (params, buffers), (batch,))
    loss = criterion(predictions, targets)
    return loss





def q(model,criterion,grad_i,x_j,y_j,gamma):
    # start_time = time.time()

    cos = torch.nn.CosineSimilarity(dim=0)
   
    # grad_i = compute_grad(x_i, y_i,criterion, model)
     

    grad_j = compute_grad(x_j, y_j, criterion,model) 
 
    arr = np.arange(len(grad_i)) 
 
    np.random.shuffle(arr)
    corr = 0
    # for i in range(int(len(arr)*0.01)):
    #     corr = corr + cos( grad_i[arr[i]].flatten(), grad_j[arr[i]].flatten() )
    with torch.no_grad():

        corr = cos( grad_i[-1].flatten(), grad_j[-1].flatten() )
    # print("---q runtime is %s seconds ---" % (time.time() - start_time))

    return max( corr-gamma ,0 )

def embedding_corr(model, output_i, x_j,gamma):
    cos = torch.nn.CosineSimilarity(dim=0)
    x_j = x_j.unsqueeze(0)  # prepend batch dimension for processing
    output = model(x_j)
    output_j = output[-1]
    corr = cos(output_i.flatten(), output_j.flatten())
    return max( corr-gamma ,0 ) 



def weighted_criterion(outputs,labels,criterion,weight):
    # start_time = time.time()

    weighted_loss = torch.tensor(0)
    weighted_loss.to(device)
    for i in range(len(outputs)):
        weighted_loss = weighted_loss + weight[i]*criterion(outputs[i],labels[i])
 
    # print("---weighted_criterion runtime is %s seconds ---" % (time.time() - start_time))

    return weighted_loss 


def train_v2(train_loader, model, criterion, optimizer, num_train, gamma, z, epoch):
    """ Run one train epoch """

    losses = utils.AverageMeter()
    accuracies = utils.AverageMeter()

    model.train()
    

    for _, (inputs, target) in enumerate(train_loader):
        target = target.to(device)
        input_var = inputs.to(device)
        target_var = target

        params = {k: v.detach() for k, v in model.named_parameters()}
        buffers = {k: v.detach() for k, v in model.named_buffers()}
        # for n, p in model.named_parameters():
        #     print('Parameter name:', n)

        partial_para = params[module.linear.weight]
        ft_compute_grad = grad(compute_loss, 1)
        ft_compute_sample_grad = vmap(ft_compute_grad, in_dims=(None, None, 0, 0))
        ft_per_sample_grads = ft_compute_sample_grad(params, partial_para, buffers, input_var, target_var)
        print(ft_per_sample_grads.shape)

        output = model(input_var)
        acc = utils.accuracy(output.data, target)

        loss = criterion(output, target_var)

        loss_r = 0
        for parameter in model.parameters():
            loss_r += torch.sum(parameter ** 2)
        loss = loss + args.weight_decay * loss_r

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        losses.update(loss.item(), inputs.size(0))
        accuracies.update(acc, inputs.size(0))

    return losses.avg, accuracies.avg



def train(train_dataset, model, criterion, optimizer,num_train,gamma,z,epoch):
    """ Run one train epoch """
    losses = utils.AverageMeter()
    accuracies = utils.AverageMeter()

    update_gap = args.update_gap
    model.train()
    num_batches = int(num_train/args.batch_size)
    arr1 = np.arange(num_train)
    arr2 = np.arange(num_train)
 
    np.random.shuffle(arr1)
    np.random.shuffle(arr2)
 
    for t in range(num_batches):
        # start_time = time.time()

        B1_idx = arr1[t*args.batch_size:(t+1)*args.batch_size]

        batch = [train_dataset[i] for i in B1_idx]
        # print("---current batch is %s ---" % arr1[t*args.batch_size:(t+1)*args.batch_size])
        B1 = list(zip(*batch))[0] 
        B1 =  torch.stack(B1)
        Y1 = list(zip(*batch))[1] 
        Y1 = torch.tensor(Y1)        
        Y1 = Y1.to(device)
        B1_var = B1.to(device)
        Y1_var = Y1


        batch = [train_dataset[i] for i in arr2[t*args.batch_size:(t+1)*args.batch_size]]
        B2 = list(zip(*batch))[0]
        B2 =  torch.stack(B2)
        Y2 = list(zip(*batch))[1]  
        Y2 = torch.tensor(Y2)       
        Y2 = Y2.to(device)
        B2_var = B2.to(device)
        Y2_var = Y2
        

        measure = args.measure
        weight = torch.ones(len(B1),device = device)
        ##### compute weights (exp of sum) #######
        if epoch<=0:          #do 700 epoch standard ERM training
            for i in range(len(B1)):
                weight[i] = math.exp(-z[B1_idx[i]])
        elif epoch%update_gap==0 :                                # update weights every 100 epochs
            for i in range(len(B1)):
                x_i,y_i = B1[i], Y1[i]
                grad_i = compute_grad(x_i, y_i, criterion, model)
                corr = 0
                for j in range(int(len(B2)*0.05)):
                    x_j,y_j = B2[j], Y2[j]
                    if measure == 0:
                        corr = corr + q(model,criterion, grad_i,x_j,y_j,gamma)
                    else:
                        x_i = x_i.unsqueeze(0)  # prepend batch dimension for processing
                        output_i = model(x_i)[-1]
                        corr = corr + embedding_corr(model,output_i,x_j,gamma)
                z[B1_idx[i]] = corr
                weight[i] = math.exp(-z[B1_idx[i]])
        else:          #do 700 epoch standard ERM training
            for i in range(len(B1)):
                weight[i] = math.exp(-z[B1_idx[i]])
             
        weight = weight.detach()
        weight = weight/weight.sum()
 
        #####compute stochastic gradients#######

 
        output = model(B1_var)
        acc = utils.accuracy(output.data, Y1_var) 
        loss = criterion(output, Y1_var)
       
        

        weighted_loss = torch.inner(loss,weight)
        # print(weighted_loss)
        loss = loss.mean()
        loss_r = 0
        for parameter in model.parameters():
            loss_r += torch.sum(parameter ** 2)
        loss = loss + args.weight_decay * loss_r
        weighted_loss =   weighted_loss + args.weight_decay * loss_r
       
        optimizer.zero_grad()
        weighted_loss.backward()
        optimizer.step()

        losses.update(loss.item(), B1.size(0))
        accuracies.update(acc, B1.size(0))
        # print("---one batch runtime is %s seconds ---" % (time.time() - start_time))

    return losses.avg, accuracies.avg


def validate(val_loader, model, criterion):
    """ Run evaluation """

    losses = utils.AverageMeter()
    accuracies = utils.AverageMeter()

    model.eval()

    with torch.no_grad():
        for _, (inputs, target) in enumerate(val_loader):
            target = target.to(device)
            input_var = inputs.to(device)
            target_var = target.to(device)

            output = model(input_var)
            loss = criterion(output, target_var)
            loss = loss.mean()
            if args.logit_adj_post:
                output = output - args.logit_adjustments

            elif args.logit_adj_train:
                loss = criterion(output + args.logit_adjustments, target_var)

            acc = utils.accuracy(output.data, target)
            losses.update(loss.item(), inputs.size(0))
            accuracies.update(acc, inputs.size(0))

    return losses.avg, accuracies.avg


if __name__ == '__main__':
    main()
