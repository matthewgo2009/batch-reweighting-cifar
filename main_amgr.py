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

parser = get_arguments()
args = parser.parse_args()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
args.device = device
exp_loc, model_loc = utils.log_folders(args)
writer = SummaryWriter(log_dir=exp_loc)


def main():
    """Main script"""

    assert not (args.logit_adj_post and args.logit_adj_train)
    train_dataset, val_loader, num_train = utils.get_loaders(args)
    num_class = len(args.class_names)
    model = torch.nn.DataParallel(resnet32(num_classes=num_class))
    model = model.to(device)
    cudnn.benchmark = True
    criterion = nn.CrossEntropyLoss().to(device)
    
    ####create z initialization#########
    z = np.zeros(num_train)
    
    gamma = 0.3
    eta = 0.01

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
        train_loss, train_acc = train(train_dataset, model, criterion, optimizer,num_train,gamma,z)
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
    
    sample = sample.unsqueeze(0)  # prepend batch dimension for processing
    target = target.unsqueeze(0)

    prediction = model(sample)
    loss = criterion(prediction, target)

    grad = torch.autograd.grad(loss, list(model.parameters()))

    # flat_grad = torch.tensor([]).to(device)
    # for item in grad:
    #     flat_grad = torch.cat((flat_grad,item.flatten()), dim=0)
    # flat_grad = torch.stack([item.flatten() for item in grad])
    return grad

def q(model,criterion,x_i,y_i,x_j,y_j,gamma):
    cos = torch.nn.CosineSimilarity(dim=0)
  

    # output_i = model(x_i)
    # loss_i = criterion(output_i, y_i)
    # loss_i.backward() 
    # grad_i = torch.autograd.grad(loss_i, list(model.parameters()))
    grad_i = compute_grad(x_i, y_i,criterion, model)
    # grad_i = grad_i/torch.norm(grad_i)

 
    # output_j = model(x_j)
    # loss_j = criterion(output_j, y_j)
    # loss_j.backward()
    # grad_j = torch.autograd.grad(loss_j, list(model.parameters()))

    grad_j = compute_grad(x_j, y_j, criterion,model) 
    # grad_j = grad_j/torch.norm(grad_j)

    arr = np.arange(len(grad_i)) 
 
    np.random.shuffle(arr)
    corr = 0
    for i in range(int(len(arr)*0.1)):
        corr = corr + cos(grad_i[arr[i]].flatten(), grad_j[arr[i]].flatten())
    # return max( torch.inner(grad_i, grad_j)-gamma ,0 )
    return max( corr-gamma ,0 )


def train(train_dataset, model, criterion, optimizer,num_train,gamma,z):
    """ Run one train epoch """
    beta = 0.2
    losses = utils.AverageMeter()
    accuracies = utils.AverageMeter()

    model.train()
    old_model=model
    num_batches = int(num_train/args.batch_size)
    arr1 = np.arange(num_train)
    arr2 = np.arange(num_train)
 
    np.random.shuffle(arr1)
    np.random.shuffle(arr2)
    for t in range(num_batches):
        batch = [train_dataset[i] for i in arr1[t*args.batch_size:(t+1)*args.batch_size]]
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
 
        
        print(111111111111)

        #####update z to approx exp of sum #######
        weight = []
        for i in range(len(B1)):
            x_i,y_i = B1[i], Y1[i]
            corr = 0
            for j in range(int(len(B2)*0.1)):
                x_j,y_j = B2[j], Y2[j]
                corr = corr + q(model,criterion, x_i,y_i,x_j,y_j,gamma) - q(old_model,criterion, x_i,y_i,x_j,y_j,gamma) 

            z[i] = (1-beta)*(z[i]+corr) + beta*corr
            weight.append(math.exp(-z[i]))


        #####compute stochastic gradients#######

        old_model = model 

        output = model(B1_var)
        acc = utils.accuracy(output.data, Y1_var) 
        loss = criterion(output, Y1_var, weight=weight)

        loss_r = 0
        for parameter in model.parameters():
            loss_r += torch.sum(parameter ** 2)
        loss = loss + args.weight_decay * loss_r
        
        # for i in range(arr1[t]*batch_size, (arr1[t]+1)*batch_size):
        #     x_i,y_i = train_dataset[i]
        #     output_i = model(x_i)
        #     loss_i = criterion(weight=weight,output_i, y_i)
        #     loss_i.backward()
        #     para = model.parameters()
        #     grad_i = para.grad
        #     weighted_grad = weighted_grad + math.exp(-z[i])*grad_i 
 
        # model.parameters() = model.parameters() - eta*weighted_grad
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        losses.update(loss.item(), inputs.size(0))
        accuracies.update(acc, inputs.size(0))


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