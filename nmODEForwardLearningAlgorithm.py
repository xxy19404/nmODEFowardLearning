import os
import numpy as np
import torch
import torchvision
import torch.nn.functional as F
from torchvision import transforms
from loguru import logger
from tqdm import tqdm
import torchdiffeq
import timm
import torch.optim as optim
from torch import nn
import pandas as pd

class Config:
    epoch = 1000
    batch_size= 256
    num_workers = 8
    device = "cuda:3" 
    torch.manual_seed(22)
    logger_name="0514_mnist.log"
    best_model_path=".pth"

    csv_train={'Epoch':[],'Batch':[],'K':[],'K_acc':[],'K_loss':[]}
    csv_test={'Epoch':[],'Acc':[]}
    csv_train_name='mnist_train.csv'
    csv_test_name='mnist_test.csv'
config = Config()


train_transforms = transforms.Compose([
        transforms.Pad(2),
        transforms.RandomCrop(28),
        transforms.RandomAffine(degrees=15),
        transforms.ToTensor(),
])
test_transforms = transforms.Compose([
    transforms.ToTensor(),
])

train_dataset = torchvision.datasets.MNIST(root='./data',
                                           train=True,
                                           transform=train_transforms,
                                           download=True)
train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                           batch_size=config.batch_size,
                                           shuffle=True)
test_dataset = torchvision.datasets.MNIST(root='./data',
                                         train=False,
                                         transform=test_transforms,
                                         download=True)
test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                         batch_size=config.batch_size,
                                         shuffle=False)
@torch.no_grad()
def adjODE(gamma, t_span, p_0, q_0):
    def f(t, y):
        p, q = y
        dp = -p + torch.pow(torch.sin(p+gamma), 2)
        dq = -q + torch.sin(2 * (p + gamma)) * (1 + q)
        return torch.stack([dp, dq])
    sol = torchdiffeq.odeint(f, torch.stack([p_0, q_0]), t_span)
    return sol[-1][0], sol[-1][1]

class OdeLayer(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, W1, b, tbar, y, q, k):
        gamma = torch.matmul(x, W1.T) + b
        y, q = adjODE(gamma, torch.tensor([(k-1)*tbar,k*tbar]), y, q)
        ctx.save_for_backward(x, y, q, W1, b)
        return y,q

    @staticmethod
    def backward(ctx, grad_y, grad_q):
        try:
            # import pdb;pdb.set_trace()
            x,y,q,W1,b = ctx.saved_tensors
        except:
            import pdb;pdb.set_trace()
            
        xi = grad_y * q
        grad_weight = torch.matmul(xi.T, x)
        grad_bias = xi.sum(dim=0)
        
        return None, grad_weight, grad_bias,None,None,None,None

class odeNetOL(nn.Module):
    def __init__(self, xsize, ysize, asize, alpha, beta, tbar, K):
        super(odeNetOL, self).__init__()
        self.xsize = xsize
        self.ysize = ysize
        self.asize = asize
        self.W1 = nn.Parameter(torch.zeros((ysize,xsize), requires_grad=True, device=config.device))
        self.W2 = nn.Parameter(torch.zeros((asize,ysize), requires_grad=True, device=config.device))
        self.b = nn.Parameter(torch.zeros(ysize, requires_grad=True, device=config.device))
        self.alpha = alpha
        self.beta = beta
        self.tbar = float(tbar)
        self.K = K
        torch.nn.init.kaiming_uniform_(self.W1, mode='fan_out', nonlinearity='relu')
        torch.nn.init.kaiming_uniform_(self.W2, mode='fan_out', nonlinearity='relu')
        self.init_state()
    
    def s(self, z):
        return F.softmax(z, dim=1)
    
    def init_state(self):
        self.y = torch.zeros((config.batch_size, self.ysize), device=config.device)
        self.q = torch.zeros((config.batch_size, self.ysize), device=config.device)
    
    def forward(self, input,k):
        self.y,self.q = OdeLayer.apply(input, self.W1, self.b, self.tbar,self.y.detach(),self.q.detach(),k)
        z = torch.matmul(self.y, self.W2.T)
        a = self.s(z)
        return a
    
    @torch.no_grad()
    def test(self, input):
        batch_size = batch[0].shape[0]
        x = batch[0].view(batch_size, -1).to(config.device) 
        d = batch[1].to(config.device)
        y = torch.zeros((config.batch_size, self.ysize), device=config.device)
        q = torch.zeros((config.batch_size, self.ysize), device=config.device)

        gamma = torch.matmul(x, self.W1.T) + self.b
        y,_ = adjODE(gamma, torch.tensor([0,self.K*self.tbar]), y, q)
        z = torch.matmul(y, self.W2.T)
        a = self.s(z)
        
        return a.detach().cpu().numpy()
    
    def save(self, path):
        torch.save({"W1": self.W1, "W2": self.W2, "b": self.b}, path)
    
    def load(self, path):
        params = torch.load(path, map_location=config.device)
        self.W1 = torch.nn.Parameter(params["W1"])
        self.W2 = torch.nn.Parameter(params["W2"])
        self.b = torch.nn.Parameter(params["b"])


if __name__=="__main__":
    net = odeNetOL(xsize=28*28, ysize=4096,  asize=10, alpha=0.05, beta=0.05, tbar=0.05, K=10)
    # net.load(config.best_model_path)
    loss = torch.nn.CrossEntropyLoss()
    logger.add(config.logger_name, level="INFO")        
    best_epoch, best_acc = -1, -1
    optimizer = optim.SGD(
                net.parameters(),             
                lr=net.alpha, momentum=0.9
                )
    for epoch_id in range(config.epoch):

        with tqdm(len(train_loader)) as pbar:
            for batch_id, batch in enumerate(train_loader):

                batch_size = batch[0].shape[0]
                data = batch[0].view(batch_size, -1).to(config.device) 
                d = batch[1].to(config.device)
                
                net.init_state()
                if batch_size != config.batch_size:
                    continue 
                
                for i in range(net.K):   
                    cnt =0.0
                    optimizer.zero_grad()
                    a = net(data,i)
                    cnt += (a.argmax(axis=1) == d).sum()
                    J = loss(a, d)

                    config.csv_train['Epoch'].append(epoch_id)
                    config.csv_train['Batch'].append(batch_id)
                    config.csv_train['K'].append(batch_id)
                    config.csv_train['K_loss'].append(J.item())

                    J.backward()
                    
                    optimizer.step()
                    batch_acc = cnt / batch_size

                    config.csv_train['K_acc'].append(batch_acc.item())

                pbar.update(1)
                pbar.set_description("Epoch: {}, Batch: {}/{}, Train Acc: {:.5f}".format(epoch_id, batch_id, len(train_loader), batch_acc))
                
        #test
        config.csv_test['Epoch'].append(epoch_id)
        total, correct = 0., 0.
        for batch_id, batch in enumerate(test_loader):
            batch_size = batch[0].shape[0]
            
            if batch_size != config.batch_size:
                continue 
            a_pred = np.argmax(net.test(batch),axis=1)
            a_true = batch[1].numpy()
            correct += np.sum(a_pred==a_true)
            total += a_true.shape[0]
        acc = correct/total
        config.csv_test['Acc'].append(acc)
        if acc >= best_acc:
            best_acc = acc
            best_epoch = epoch_id
            logger.info("Epoch: %d, Test Acc improved to: %.5f" % (epoch_id, acc))
            net.save(f"mnist_0514_K={net.K}_{best_acc:.4f}.pth")
        else:
            logger.info("Epoch: %d, Test Acc is: %.5f, Best Test Acc is: %.5f in epoch: %d" % (epoch_id, acc, best_acc, best_epoch))
    
    df_train=pd.DataFrame(config.csv_train)
    df_test=pd.DataFrame(config.csv_test)
    df_train.to_csv(config.csv_train_name,index=False)
    df_test.to_csv(config.csv_test_name,index=False)
    
